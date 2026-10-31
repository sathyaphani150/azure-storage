# StockLens: Inventory Document Intelligence

An event-driven Azure Storage POC for supplier delivery notes and invoices. A user uploads a PDF, Azure Blob Storage raises an Event Grid event, Event Grid sends it to Azure Queue Storage, and an Azure Function extracts selectable PDF text to another Blob container. Azure Table Storage holds the status and metadata. The UI polls the Function API to show the result.

## Architecture

`Static website → FastAPI on Azure Functions → incoming-pdfs Blob → Event Grid → pdf-processing Queue → Python queue worker → extracted-text Blob + DocumentProcessing Table → UI`

This demonstrates a practical inventory workflow: operations teams can upload a supplier's delivery note and quickly search the extracted text for SKU, quantity, batch, or purchase order data. It deliberately preserves the source PDF, so processing is replayable and auditable.

## 1. Create the Azure resources in the portal

Use one resource group, such as `rg-stocklens-poc`, in a single region.

1. Create a **Storage account** (Standard, general-purpose v2, locally redundant storage is fine for a POC). Under **Data storage > Containers**, create private containers named `incoming-pdfs` and `extracted-text`. Under **Data storage > Queues**, create `pdf-processing`. Under **Data storage > Static website**, enable it and save the primary endpoint.
2. Create a **Function App**: **Python 3.11**, Consumption/Flex Consumption, same resource group/region. Attach the storage account above. In **Configuration**, add the app settings shown in `local.settings.example.json`, replacing both connection strings with the storage account connection string. For the POC, use the connection string only in Function settings; never put it in the website.
3. Create the Event Grid subscription from Storage account > **Events**. Event type: **Blob Created**. Subject filter: begins with `/blobServices/default/containers/incoming-pdfs/`. Endpoint type: **Storage Queue**. Select queue `pdf-processing`. Leave the event schema as **Event Grid Schema**.
4. On the Function App, set `ALLOWED_ORIGIN` to your static website primary endpoint. Keep HTTP authorization level at Function; retrieve the default function key after deploy.

## 2. Run and deploy the Function

```powershell
Copy-Item local.settings.example.json local.settings.json
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
func start
func azure functionapp publish <your-function-app-name>
```

When the host starts, open `http://localhost:7071/api/`—not `http://localhost:7071/api/%7B*route%7D`. The Function serves the dashboard from the `web/` folder.

### Local fallback when Core Tools cannot start

If `func start` fails before listing functions because of a Core Tools extension-bundle or language-worker error, run the same dashboard and queue-processing logic directly:

```powershell
python -m pip install -r requirements.txt
python local_run.py
```

Open `http://localhost:7071/`. This runner polls `pdf-processing` using the same `process_event` function called by the deployed Azure Queue-triggered Function; it is intended only for local verification.

For local development, start Azurite before `func start`, or replace the two connection-string values in `local.settings.json` with your Azure Storage account connection string.

## 3. Deploy and configure the UI

1. In `web/app.js`, replace `REPLACE_WITH_FUNCTION_API_BASE` with `https://<your-function-app-name>.azurewebsites.net/api`.
2. In the same file, replace `REPLACE_WITH_FUNCTION_KEY` with the Function host key. This is adequate for an internal POC; use Microsoft Entra ID authentication and a backend-for-frontend for production.
3. Upload the contents of `web/` to the special `$web` container in Storage Explorer or Azure Portal. Open the static website endpoint.

## Demo script

1. Upload a delivery-note PDF containing item codes and quantities.
2. Show the `Uploaded` row; open Storage account > Events and Queue to make the event-driven handoff visible.
3. Refresh the UI. It changes to `Processing`, then `Completed`, and displays extracted text.
4. In the portal, show the original under `incoming-pdfs`, text under `extracted-text`, and metadata/status in Storage Browser > Tables > `DocumentProcessing`.

## POC boundaries

`pypdf` extracts embedded/selectable PDF text. Scanned documents need OCR; the high-value next step is Azure AI Document Intelligence's prebuilt invoice model to return SKU/quantity fields and add a low-stock rule.
