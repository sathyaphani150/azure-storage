"""StockLens: FastAPI HTTP API and Azure Queue-triggered PDF worker."""
import json
import os
import re
import uuid
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote, urlparse

import azure.functions as func
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.data.tables import TableServiceClient, UpdateMode
from azure.storage.blob import BlobServiceClient, ContentSettings
from azure.storage.queue import QueueClient
from docx import Document
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pypdf import PdfReader

MAX_FILE_BYTES = 10 * 1024 * 1024
PARTITION_KEY = "documents"
SUPPORTED_DOCUMENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
function_app = func.FunctionApp()
api = FastAPI(title="StockLens API", docs_url=None, redoc_url=None)
api.add_middleware(CORSMiddleware, allow_origins=[os.getenv("ALLOWED_ORIGIN", "*")], allow_credentials=False, allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["Content-Type"])


def setting(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if not value:
        raise RuntimeError(f"Missing required app setting: {name}")
    return value


def connection_string() -> str:
    return setting("STORAGE_CONNECTION_STRING", os.getenv("AzureWebJobsStorage"))


def incoming_container() -> str:
    return setting("INCOMING_CONTAINER", "incoming-pdfs")


def extracted_container() -> str:
    return setting("EXTRACTED_CONTAINER", "extracted-text")


def table_name() -> str:
    return setting("TABLE_NAME", "DocumentProcessing")


def blob_service() -> BlobServiceClient:
    return BlobServiceClient.from_connection_string(connection_string())


def table_client():
    return TableServiceClient.from_connection_string(connection_string()).get_table_client(table_name())


def ensure_infrastructure() -> None:
    service = blob_service()
    for container in (incoming_container(), extracted_container()):
        try:
            service.create_container(container)
        except ResourceExistsError:
            pass
    TableServiceClient.from_connection_string(connection_string()).create_table_if_not_exists(table_name())


def queue_processing_event(blob_url: str) -> None:
    """Queue the same event shape Event Grid emits for reliable local demos."""
    event = {"eventType": "Microsoft.Storage.BlobCreated", "data": {"url": blob_url}}
    QueueClient.from_connection_string(connection_string(), "pdf-processing").send_message(json.dumps(event))


def safe_filename(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]", "_", value)[-180:] or "document.pdf"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def document_entity(document_id: str) -> dict:
    try:
        return dict(table_client().get_entity(partition_key=PARTITION_KEY, row_key=document_id))
    except ResourceNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Document not found") from exc


@api.post("/documents", status_code=201)
async def upload_document(file: UploadFile = File(...)):
    filename = file.filename or "document.pdf"
    extension = Path(filename).suffix.lower()
    if extension not in SUPPORTED_DOCUMENT_TYPES:
        raise HTTPException(status_code=400, detail="Only PDF and DOCX uploads are allowed.")
    contents = await file.read(MAX_FILE_BYTES + 1)
    if not contents:
        raise HTTPException(status_code=400, detail="Choose a PDF file.")
    if len(contents) > MAX_FILE_BYTES:
        raise HTTPException(status_code=400, detail="PDF must be 10 MB or smaller.")

    ensure_infrastructure()
    document_id = str(uuid.uuid4())
    blob_name = f"{document_id}/{datetime.now(timezone.utc):%Y%m%d%H%M%S}-{safe_filename(filename)}"
    table = table_client()
    # Create this first: Event Grid can fire immediately after Blob upload.
    file_type = SUPPORTED_DOCUMENT_TYPES[extension]
    table.create_entity({"PartitionKey": PARTITION_KEY, "RowKey": document_id, "documentId": document_id, "fileName": filename, "fileType": file_type, "uploadedAt": utc_now(), "status": "Uploading", "blobName": blob_name, "extractedText": "", "errorMessage": ""})
    try:
        blob_client = blob_service().get_blob_client(incoming_container(), blob_name)
        blob_client.upload_blob(contents, overwrite=False, metadata={"documentid": document_id}, content_settings=ContentSettings(content_type=file_type))
        table.update_entity({"PartitionKey": PARTITION_KEY, "RowKey": document_id, "status": "Uploaded"}, mode=UpdateMode.MERGE)
        if os.getenv("LOCAL_QUEUE_ON_UPLOAD", "").lower() in {"1", "true", "yes"}:
            queue_processing_event(blob_client.url)
    except Exception as exc:
        table.update_entity({"PartitionKey": PARTITION_KEY, "RowKey": document_id, "status": "Failed", "errorMessage": str(exc)[:1000]}, mode=UpdateMode.MERGE)
        raise HTTPException(status_code=500, detail="Upload failed. Check Function logs.") from exc
    return {"documentId": document_id, "status": "Uploaded", "message": "Saved and queued for processing."}


@api.get("/documents")
def list_documents():
    ensure_infrastructure()
    documents = [dict(entity) for entity in table_client().query_entities(f"PartitionKey eq '{PARTITION_KEY}'")]
    documents.sort(key=lambda entity: entity.get("uploadedAt", ""), reverse=True)
    return {"documents": documents}


@api.get("/documents/{document_id}/download")
def download_original(document_id: str):
    entity = document_entity(document_id)
    try:
        contents = blob_service().get_blob_client(incoming_container(), entity["blobName"]).download_blob().readall()
    except ResourceNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Original file not found") from exc
    return Response(content=contents, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{safe_filename(entity["fileName"])}"'})


# The same dashboard works locally and after the Function App is deployed.
# API routes are registered above this mount, so /documents still reaches FastAPI.
api.mount("/", StaticFiles(directory=Path(__file__).parent / "web", html=True), name="dashboard")


@function_app.route(route="{*route}", methods=["GET", "POST", "OPTIONS"], auth_level=func.AuthLevel.ANONYMOUS)
async def fastapi_handler(req: func.HttpRequest, context: func.Context) -> func.HttpResponse:
    return await func.AsgiMiddleware(api).handle_async(req, context)


def event_from_message(message: func.QueueMessage) -> dict:
    payload = json.loads(message.get_body().decode("utf-8"))
    return payload[0] if isinstance(payload, list) else payload


@function_app.queue_trigger(arg_name="message", queue_name="pdf-processing", connection="AzureWebJobsStorage")
def process_document(message: func.QueueMessage) -> None:
    event = event_from_message(message)
    process_event(event)


def process_event(event: dict) -> None:
    """Core processing logic, shared by the Azure Queue trigger and local dev runner."""
    if event.get("eventType") and event["eventType"] != "Microsoft.Storage.BlobCreated":
        return
    blob_url = event.get("data", {}).get("url")
    if not blob_url:
        raise ValueError("Queue event has no blob URL. Use Event Grid schema for the subscription.")
    marker = f"/{incoming_container()}/"
    path = urlparse(blob_url).path
    if marker not in path:
        raise ValueError(f"Unexpected blob URL: {blob_url}")
    blob_name = unquote(path.split(marker, 1)[1])
    document_id = blob_name.split("/", 1)[0]
    table = table_client()
    try:
        entity = dict(table.get_entity(partition_key=PARTITION_KEY, row_key=document_id))
    except ResourceNotFoundError:
        return  # Ignore blobs not uploaded through this application.
    if entity.get("status") == "Completed":
        return
    table.update_entity({"PartitionKey": PARTITION_KEY, "RowKey": document_id, "status": "Processing", "errorMessage": ""}, mode=UpdateMode.MERGE)
    try:
        source = blob_service().get_blob_client(incoming_container(), blob_name).download_blob().readall()
        extension = Path(blob_name).suffix.lower()
        if extension == ".pdf":
            reader = PdfReader(BytesIO(source))
            text = "\n".join((page.extract_text() or "") for page in reader.pages).strip()
        elif extension == ".docx":
            document = Document(BytesIO(source))
            text = "\n".join(paragraph.text for paragraph in document.paragraphs if paragraph.text.strip()).strip()
        else:
            raise ValueError("Unsupported document type. Only PDF and DOCX documents can be processed.")
        if not text:
            text = "[No extractable text found. Scanned PDFs need OCR; add Azure AI Document Intelligence for production.]"
        text_blob_name = f"{document_id}.txt"
        blob_service().get_blob_client(extracted_container(), text_blob_name).upload_blob(text.encode("utf-8"), overwrite=True, content_settings=ContentSettings(content_type="text/plain; charset=utf-8"))
        table.update_entity({"PartitionKey": PARTITION_KEY, "RowKey": document_id, "status": "Completed", "processedAt": utc_now(), "textBlobName": text_blob_name, "extractedText": text[:16000], "errorMessage": ""}, mode=UpdateMode.MERGE)
    except Exception as exc:
        table.update_entity({"PartitionKey": PARTITION_KEY, "RowKey": document_id, "status": "Failed", "errorMessage": str(exc)[:1000]}, mode=UpdateMode.MERGE)
        raise
