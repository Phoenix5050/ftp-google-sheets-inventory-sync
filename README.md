# ftp-google-sheets-inventory-sync

# Serverless Inventory Data Ingestion & Sync Pipeline

A serverless data integration pipeline built with Python, Flask, Docker, and GCP Cloud Run. It automates the retrieval of raw vehicle inventory data from external FTP servers, cleanses and merges the records with VDP links sourced from Google Sheets, and updates target inventory destinations. The pipeline automatically purges stale records to maintain data consistency and logs execution metrics directly to Google Drive. Fully configurable via environment variables, this service runs securely on a scheduled Cloud Run web service trigger.
