import asyncio
import os
from datetime import datetime
from typing import Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from health import run_health_checks
from llama_inference import LlamaInference
from llmhub_inference import LLMHubInference
from loguru import logger
from pydantic import BaseModel
from pymongo import MongoClient
from sagemaker_inference import SageMakerInference
from t5_inference import T5Inference

load_dotenv()

app = FastAPI()
client = MongoClient(os.getenv("MONGO_URI"))
db = client[os.getenv("MONGO_DB_NAME")]
collection = db[os.getenv("MONGO_COLLECTION_NAME")]

origins = ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class Response(BaseModel):
    """
    Response model returned by the `/transform-sentence-to-imr` endpoint.

    Attributes:
        timestamp (str): Time when the inference was made.
        imr (Dict): The final IMR (intermediate representation) result.
        inputSentence (str): The input sentence, lowercased.
        status (str): Status of the request (e.g., 'success', 'error').
        rawOutput (object): Raw model output before adaptation.
        modelVersion (str): Identifier for which model was used.
        error (Optional[str]): Optional error message (only present on error).
        prompt (Optional[str]): Optional prompt sent to the model.
    """

    timestamp: str
    imr: Dict
    display: List[Dict]
    inputSentence: str
    rawOutput: object
    status: str
    modelVersion: str
    error: Optional[str] = None
    prompt: Optional[str] = None


class HTTPErrorResponse(BaseModel):
    """
    Response model returned in case of an HTTP error.

    Attributes:
        message (str): Description of the error.
        status (str): Error status indicator (e.g., 'error').
    """

    message: str
    status: str


class RequestBody(BaseModel):
    """
    Expected request body for the `/transform-sentence-to-imr` endpoint.

    Attributes:
        sentence (str): Sentence to be processed by the model.
        model (str): Model key to use (e.g., 'llama', 't5').
        username (str): Username of the requester.
        environment (str): Execution environment (e.g., dev, prod).
    """

    sentence: str
    model: str
    username: str
    environment: str


@app.get("/health")
async def health() -> JSONResponse:
    """
    Report whether this service can actually do its job.

    Returns 200 while every critical dependency answers and 503 otherwise, so
    an uptime check needs no body parsing, while the body names each
    dependency with its latency so an alert can say what broke.

    Unlike /docs, this exercises the dependencies a transformation needs, and
    performs no inference, so it is cheap enough to poll continuously.
    """
    report, healthy = await run_health_checks(client)
    return JSONResponse(
        content=report,
        status_code=status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE,
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """
    Custom handler for HTTP exceptions.

    Args:
        request (Request): Incoming HTTP request (not used directly).
        exc (HTTPException): The raised HTTP exception.

    Returns:
        JSONResponse: A structured JSON error response with status 400.
    """
    response_model = HTTPErrorResponse(status="error", message=str(exc.detail))
    json_compatible_item_data = jsonable_encoder(response_model)
    return JSONResponse(
        status_code=400,
        content=json_compatible_item_data,
    )


MODEL_INFERENCES = {
    "llama": LlamaInference(),
    "t5": T5Inference(),
    # "sagemaker": SageMakerInference(),
    "llmhub": LLMHubInference(),
}

MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "10"))


async def log_to_mongodb(document: dict) -> None:
    """
    Persist an inference result (or failure) to MongoDB.

    Runs the blocking pymongo insert in a thread so the async handler
    is not stalled. Failures are logged but never propagated, so a
    MongoDB outage cannot break the inference response.

    Args:
        document (dict): The document to insert into the collection.
    """
    try:
        await asyncio.to_thread(collection.insert_one, dict(document))
    except Exception as e:
        logger.error(f"Failed to log inference result to MongoDB: {e}")


@app.post(
    "/transform-sentence-to-imr",
    response_model=Response,
    status_code=status.HTTP_200_OK,
)
async def transform_sentence_to_imr(body: RequestBody):
    """
    Transforms an input sentence into an intermediate representation (IMR)
    using the specified model ('llama' or 't5').

    Retries up to MAX_ENDPOINT_RETRIES times until a 200 response is received,
    using exponential backoff between attempts.

    Args:
        body (RequestBody): Request payload containing input sentence,
            model name, username, and environment.

    Returns:
        dict: A dictionary with the inference result and metadata.

    Raises:
        HTTPException: If all retries are exhausted without a 200 response.
    """
    sentence = body.sentence.lower()
    environment = body.environment
    model = body.model
    username = body.username

    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = await MODEL_INFERENCES[model].generate(sentence, environment)

            if response.status_code == status.HTTP_200_OK:
                raw_output = MODEL_INFERENCES[model].get_raw_output(response)
                adopted_result = await MODEL_INFERENCES[model].adopt(raw_output)

                logger.info(f"Endpoint attempt {attempt} succeeded.")
                model_result = {
                    "timestamp": f"{datetime.now():%Y-%m-%d %H:%M:%S%z}",
                    "inputSentence": sentence,
                    "imr": adopted_result["imr"],
                    "display": adopted_result["display"],
                    "rawOutput": raw_output,
                    "modelVersion": model,
                    "status": "success",
                    "username": username,
                }
                await log_to_mongodb(model_result)
                return model_result

            raise Exception(
                f"Model returned status {response.status_code} on attempt {attempt}."
            )

        except Exception as e:
            last_error = e
            wait = 2 ** (attempt - 1)  # 1s, 2s, 4s, 8s, 16s
            logger.warning(
                f"Endpoint attempt {attempt}/{MAX_RETRIES} failed: {e}. Retrying in {wait}s..."
            )
            if attempt < MAX_RETRIES:
                await asyncio.sleep(wait)

    logger.error(
        f"Endpoint failed after {MAX_RETRIES} attempts. Last error: {last_error}"
    )
    await log_to_mongodb(
        {
            "timestamp": f"{datetime.now():%Y-%m-%d %H:%M:%S%z}",
            "inputSentence": sentence,
            "modelVersion": model,
            "status": "error",
            "error": str(last_error),
            "username": username,
        }
    )
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=f"Inference failed after {MAX_RETRIES} attempts. Last error: {last_error}",
    )
