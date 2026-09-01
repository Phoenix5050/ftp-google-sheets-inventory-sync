# --- INITIAL SETUP & IMPORTS ---
import os
import logging
import datetime
import json
import socket
import pandas as pd
import gspread
from ftplib import FTP
from gspread_dataframe import set_with_dataframe
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from flask import Flask, request

# --- CORE LOGGING SETUP ---
LOG_FORMAT = '%(asctime)s - %(levelname)s - %(message)s'
logger = logging.getLogger()
logger.setLevel(logging.INFO)

for handler in logger.handlers[:]:
    logger.removeHandler(handler)
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt='%Y-%m-%d %H:%M:%S'))
logger.addHandler(stream_handler)

# --- FLASK SETUP ---
app = Flask(__name__)

# Set default socket timeout to 600 seconds (10 minutes)
socket.setdefaulttimeout(600)

# --- GLOBAL HELPER FUNCTIONS ---

def upload_file_to_drive(drive_service, local_file_path, drive_folder_id, logger):
    """
    Uploads a file directly to a target Folder ID (Shared Drive ID).
    Uses supportsAllDrives=True to enforce Shared Drive quota usage.
    """
    if not drive_service:
        logger.warning(f"Drive API v3 service is not initialized. Skipping upload for {local_file_path}.")
        return

    if not drive_folder_id or len(drive_folder_id) < 20:
        logger.error(f"DRIVE_TARGET_FOLDER_ID is invalid or missing. Skipping Drive upload for {local_file_path}.")
        return

    try:
        file_name = os.path.basename(local_file_path)

        # Search for existing file within the target folder ID
        file_query = f"name='{file_name}' and '{drive_folder_id}' in parents and trashed=false"
        response = drive_service.files().list(
            q=file_query,
            fields='files(id)',
            supportsAllDrives=True,
            includeItemsFromAllDrives=True
        ).execute(num_retries=3)
        file_id = response.get('files')[0]['id'] if response.get('files') else None

        # Upload/Update logic
        media = MediaFileUpload(local_file_path, resumable=True)
        file_metadata = {'name': file_name}

        if file_id:
            drive_service.files().update(
                fileId=file_id,
                body=file_metadata,
                media_body=media,
                fields='id',
                supportsAllDrives=True
            ).execute(num_retries=3)
            logger.info(f"Existing file '{file_name}' updated successfully in Shared Drive (ID: {drive_folder_id}).")
        else:
            file_metadata['parents'] = [drive_folder_id]
            drive_service.files().create(
                body=file_metadata,
                media_body=media,
                fields='id',
                supportsAllDrives=True
            ).execute(num_retries=3)
            logger.info(f"New file '{file_name}' uploaded successfully to Shared Drive (ID: {drive_folder_id}).")

    except Exception as e:
        logger.error(f"FATAL DRIVE FAILURE: Could not save {local_file_path} to Shared Drive. Error: {e}")
        raise

def prepend_and_save_log(df_new, df_used, start_time, log_filename, drive_folder_id):
    """
    Generates separate NEW and USED log entries, prepends them to the file,
    and copies the updated log to Drive using the Shared Drive ID.
    """

    def generate_single_log(df, inventory_type_label, run_time, start_time_ref):
        current_duration = datetime.datetime.now() - start_time_ref

        missing_df = df[df.get('VDP_URL').isna()][['StockNumber', 'VDP_URL']].copy() if 'VDP_URL' in df.columns else pd.DataFrame()
        num_missing = len(missing_df)
        total_records = len(df)

        CLIENT_NAME = os.environ.get('CLIENT_NAME', 'Dealer')
        log_entry_parts = [
            "="*70,
            f"RUN COMPLETE: {run_time} ({inventory_type_label} {CLIENT_NAME} Inventory)",
            f"Duration: {current_duration}",
            f"Total Records Merged: {total_records}",
            f"Records Missing VDP URL: {num_missing}",
            ""
        ]

        if num_missing > 0:
            if 'StockNumber' in missing_df.columns:
                missing_data_str = missing_df.head(50).to_string(index=False, header=True)
            else:
                missing_data_str = missing_df.head(50).to_string(index=True, header=True)

            log_entry_parts.extend([
                "--- MISSING VDP RECORDS (First 50) ---",
                missing_data_str,
                "--------------------------------------"
            ])
            if num_missing > 50:
                log_entry_parts.append(f"-- Total missing records: {num_missing}. Check file for complete list.")

        return "\n".join(log_entry_parts) + "\n"

    run_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    log_new = generate_single_log(df_new, "NEW", run_time, start_time)
    log_used = generate_single_log(df_used, "USED", run_time, start_time)
    combined_log_entry = log_new + log_used

    old_content = ""
    os.makedirs(os.path.dirname(log_filename) or '.', exist_ok=True)
    if os.path.exists(log_filename):
        try:
            with open(log_filename, 'r') as f:
                old_content = f.read()
        except Exception as e:
            logger.error(f"Failed to read existing log file: {e}")

    new_content = combined_log_entry + old_content

    with open(log_filename, 'w') as f:
        f.write(new_content)

    logger.info(f"Separated NEW and USED log entries prepended to {log_filename}")

    upload_file_to_drive(globals().get('drive_api'), log_filename, drive_folder_id, logger)

    return combined_log_entry

def download_from_ftp(server, username, password, remote_file, local_file):
    """Connects to FTP with a password and downloads a file."""
    try:
        with FTP(server, user=username, passwd=password) as ftp:
            logger.info(f"Connected to {server} for download")
            with open(local_file, 'wb') as f:
                ftp.retrbinary(f"RETR {remote_file}", f.write)
            logger.info(f"Successfully downloaded {remote_file} to {local_file}")
    except Exception as e:
        logger.error(f"Error downloading {remote_file}: {e}")
        raise

def upload_to_ftp_final(server, username, password, local_file, remote_file, logger):
    """Connects to FTP and uploads a file."""
    try:
        with FTP(server, user=username, passwd=password) as ftp:
            logger.info(f"Connected to {server} for final inventory upload")
            with open(local_file, 'rb') as f:
                ftp.storbinary(f"STOR {remote_file}", f)
            logger.info(f"Successfully uploaded {local_file} to {remote_file}")
    except Exception as e:
        logger.error(f"Error uploading {local_file}: {e}")
        raise

def cleanup_google_sheet(df_urls_original, df_inventory, worksheet, logger, tab_name):
    """
    Cleans up Google Sheet entries to remove sold or removed vehicles.
    """
    logger.info(f"Starting cleanup for Sheet Tab: '{tab_name}'...")
    if 'Vehicle Image' not in df_urls_original.columns:
        logger.error(f"Cannot clean sheet '{tab_name}': 'Vehicle Image' column missing.")
        return

    df_urls_original['Cleaned_Image'] = (
        df_urls_original['Vehicle Image'].astype(str).str.split('?').str[0].str.replace('-420x315', '', regex=False).str.strip()
    )

    image_urls_in_inventory = set(df_inventory['MainPhoto'].dropna().unique())

    df_filtered = df_urls_original[
        df_urls_original['Cleaned_Image'].isin(image_urls_in_inventory)
    ].copy()

    df_filtered.drop(columns=['Cleaned_Image'], inplace=True)

    initial_count = len(df_urls_original)
    final_count = len(df_filtered)
    removed_count = initial_count - final_count

    if initial_count > 0:
        logger.info(f"Sheet '{tab_name}': Initial records: {initial_count}, Kept records: {final_count}, Removed stale: {removed_count}")
    else:
        logger.warning(f"Sheet '{tab_name}' was empty. No cleanup performed.")
        return

    if final_count == 0 and initial_count > 0:
        logger.error(f"FATAL WARNING: Cleanup resulted in 0 records for '{tab_name}'. SKIPPING CLEAR AND WRITE to prevent data loss.")
        return

    worksheet.clear()
    logger.info(f"Cleared existing data from '{tab_name}'.")

    set_with_dataframe(worksheet, df_filtered, include_index=False, include_column_header=True)
    logger.info(f"Successfully repopulated '{tab_name}' with {final_count} up-to-date records.")

# --- CORE EXECUTION FUNCTION ---
@app.route('/', methods=['POST'])
def scheduled_job():
    """
    Main scheduled job handler triggered via HTTP POST request.
    Reads environment variables for all sensitive configuration.
    """
    global drive_api, CLIENT_NAME, DRIVE_TARGET_FOLDER_ID

    start_time = datetime.datetime.now()
    logger.info("--- Cloud Run Script Execution Started ---")

    try:
        # Service Account Key from Secret Manager / Environment
        SA_KEY_JSON = os.environ.get('SA_KEY_JSON')
        if not SA_KEY_JSON: 
            raise ValueError("SA_KEY_JSON environment variable missing.")

        # FTP Credentials
        FTP_SERVER = os.environ.get('FTP_SERVER', 'ftp.example.com')
        FTP_SERVER_MERGED_FILES = os.environ.get('FTP_SERVER_MERGED_FILES', 'ftp.example.com')
        USED_FTP_USER = os.environ.get('USED_FTP_USER', 'user_used')
        NEW_FTP_USER = os.environ.get('NEW_FTP_USER', 'user_new')
        FTP_UN_VDP = os.environ.get('FTP_UN_VDP', 'user_vdp')
        USED_FTP_PASS = os.environ.get('USED_FTP_PASS')
        NEW_FTP_PASS = os.environ.get('NEW_FTP_PASS')
        VDP_FTP_PASS = os.environ.get('VDP_FTP_PASS')

        if not all([USED_FTP_PASS, NEW_FTP_PASS, VDP_FTP_PASS]):
            raise ValueError("One or more FTP password environment variables are missing.")

        # Business Configuration
        CLIENT_NAME = os.environ.get('CLIENT_NAME', 'Sample Dealer')
        CLIENT_COMPANY_ID = os.environ.get('CLIENT_COMPANY_ID', 'COMPANY_ID')
        
        # Google Sheet Configuration
        SHEET_ID_NEW = os.environ.get('SHEET_ID_NEW')
        SHEET_ID_USED = os.environ.get('SHEET_ID_USED')
        TAB_NAME_NEW = os.environ.get('TAB_NAME_NEW', 'New Inventory')
        TAB_NAME_USED = os.environ.get('TAB_NAME_USED', 'Used Inventory')
        VDP_COLUMN_NEW = os.environ.get('VDP_COLUMN_NEW', 'Vehicle Link')
        VDP_COLUMN_USED = os.environ.get('VDP_COLUMN_USED', 'Vehicle Link')

        if not SHEET_ID_NEW or not SHEET_ID_USED:
            raise ValueError("Google Sheet ID environment variables missing.")

        # Local & Remote Files
        LOG_FILENAME = os.environ.get('LOG_FILENAME', 'inventory_merge_log.txt')
        LOCAL_USED_FILE = os.environ.get('LOCAL_USED_FILE', 'used_inventory.csv')
        LOCAL_NEW_FILE = os.environ.get('LOCAL_NEW_FILE', 'new_inventory.csv')
        USED_FTP_FILE = os.environ.get('USED_FTP_FILE', 'used_remote.csv')
        NEW_FTP_FILE = os.environ.get('NEW_FTP_FILE', 'new_remote.csv')

        # Shared Drive Folder
        DRIVE_TARGET_FOLDER_ID = os.environ.get('DRIVE_TARGET_FOLDER_ID')
        if not DRIVE_TARGET_FOLDER_ID: 
            raise ValueError("DRIVE_TARGET_FOLDER_ID environment variable missing.")

    except Exception as e:
        logger.error(f"FATAL ERROR: Configuration loading failed. Error: {e}")
        return f"Configuration Error: {e}", 500

    # Authentication
    global gc
    drive_api = None

    try:
        sa_key_dict = json.loads(SA_KEY_JSON)

        # 1. Google Sheets Authentication
        gc = gspread.service_account_from_dict(sa_key_dict)
        logger.info("Service Account (gspread) authentication successful.")

        # 2. Google Drive API Authentication
        DRIVE_SCOPES = ['https://www.googleapis.com/auth/drive']
        creds = Credentials.from_service_account_info(sa_key_dict, scopes=DRIVE_SCOPES)
        drive_service = build('drive', 'v3', credentials=creds)
        drive_api = drive_service
        logger.info("Service Account (Drive API v3) initialized successfully.")

    except Exception as e:
        logger.error(f"FATAL ERROR: Authentication failed. Error: {e}")
        return f"Authentication Error: {e}", 500

    # Core Execution
    try:
        # Download files
        download_from_ftp(FTP_SERVER, NEW_FTP_USER, NEW_FTP_PASS, NEW_FTP_FILE, LOCAL_NEW_FILE)
        download_from_ftp(FTP_SERVER, USED_FTP_USER, USED_FTP_PASS, USED_FTP_FILE, LOCAL_USED_FILE)

        df_new_inventory = pd.read_csv(LOCAL_NEW_FILE, delimiter='|', on_bad_lines='skip', dtype=str)
        df_used_inventory = pd.read_csv(LOCAL_USED_FILE, delimiter='|', on_bad_lines='skip', dtype=str)
        logger.info("Raw inventory files loaded.")

        df_new_inventory.columns = df_new_inventory.columns.str.strip()
        df_used_inventory.columns = df_used_inventory.columns.str.strip()

        df_new_inventory = df_new_inventory[df_new_inventory['CompanyID'] == CLIENT_COMPANY_ID].copy()
        df_used_inventory = df_used_inventory[df_used_inventory['CompanyID'] == CLIENT_COMPANY_ID].copy()
        logger.info(f"Inventory filtered for CompanyID {CLIENT_COMPANY_ID}.")

        spreadsheet_NEW = gc.open_by_key(SHEET_ID_NEW)
        spreadsheet_USED = gc.open_by_key(SHEET_ID_USED)

        worksheet_NEW_TAB = spreadsheet_NEW.worksheet(TAB_NAME_NEW)
        worksheet_USED_TAB = spreadsheet_USED.worksheet(TAB_NAME_USED)

        df_new_urls_original = pd.DataFrame(worksheet_NEW_TAB.get_all_records())
        df_used_urls_original = pd.DataFrame(worksheet_USED_TAB.get_all_records())

        df_new_urls_original.columns = df_new_urls_original.columns.str.strip()
        df_used_urls_original.columns = df_used_urls_original.columns.str.strip()

        logger.info("Google Sheet data loaded for processing.")

        def prepare_url_dataframe(df, vdp_column_name, inventory_type):
            if df.empty or 'Vehicle Image' not in df.columns or vdp_column_name not in df.columns:
                logger.warning(f"Skipping merge prep for {inventory_type}: Missing columns or empty.")
                return pd.DataFrame(columns=['ImageURL', 'VDP_URL'])

            df_clean = df[['Vehicle Image', vdp_column_name]].copy()
            df_clean.rename(columns={'Vehicle Image': 'ImageURL', vdp_column_name: 'VDP_URL'}, inplace=True)

            df_clean['ImageURL'] = df_clean['ImageURL'].astype(str).str.split('?').str[0].str.replace('-420x315', '', regex=False).str.strip()
            df_clean = df_clean[~df_clean['ImageURL'].str.contains('placeholder.jpg', na=False)].copy()
            df_clean = df_clean[(df_clean['ImageURL'] != '') & (df_clean['VDP_URL'] != '')].dropna(subset=['ImageURL', 'VDP_URL'])
            df_clean.drop_duplicates(subset=['ImageURL'], keep='first', inplace=True)

            return df_clean

        df_new_urls_clean = prepare_url_dataframe(df_new_urls_original, VDP_COLUMN_NEW, "New Inventory")
        df_used_urls_clean = prepare_url_dataframe(df_used_urls_original, VDP_COLUMN_USED, "Used Inventory")

        df_new_inventory['MainPhoto'] = df_new_inventory['MainPhoto'].astype(str).str.split('?').str[0].str.strip()
        df_used_inventory['MainPhoto'] = df_used_inventory['MainPhoto'].astype(str).str.split('?').str[0].str.strip()

        df_new_merged = pd.merge(df_new_inventory, df_new_urls_clean, left_on='MainPhoto', right_on='ImageURL', how='left')
        df_used_merged = pd.merge(df_used_inventory, df_used_urls_clean, left_on='MainPhoto', right_on='ImageURL', how='left')

        for df in [df_new_merged, df_used_merged]:
            if 'VDP_URL' in df.columns and 'ImageURL' in df.columns:
                core_cols = [col for col in df.columns if col not in ['ImageURL', 'VDP_URL']]
                df.drop(columns=['ImageURL'], inplace=True)
                df.columns = core_cols + ['VDP_URL']

        df_new_merged.to_csv(LOCAL_NEW_FILE, sep='|', index=False)
        df_used_merged.to_csv(LOCAL_USED_FILE, sep='|', index=False)
        logger.info("Data merging and local saving complete.")

        cleanup_google_sheet(df_new_urls_original, df_new_inventory, worksheet_NEW_TAB, logger, TAB_NAME_NEW)
        cleanup_google_sheet(df_used_urls_original, df_used_inventory, worksheet_USED_TAB, logger, TAB_NAME_USED)

        upload_to_ftp_final(FTP_SERVER_MERGED_FILES, FTP_UN_VDP, VDP_FTP_PASS, LOCAL_NEW_FILE, LOCAL_NEW_FILE, logger)
        upload_to_ftp_final(FTP_SERVER_MERGED_FILES, FTP_UN_VDP, VDP_FTP_PASS, LOCAL_USED_FILE, LOCAL_USED_FILE, logger)

        final_log_content = prepend_and_save_log(
            df_new_merged,
            df_used_merged,
            start_time,
            LOG_FILENAME,
            DRIVE_TARGET_FOLDER_ID,
        )

        upload_file_to_drive(drive_api, LOCAL_NEW_FILE, DRIVE_TARGET_FOLDER_ID, logger)
        upload_file_to_drive(drive_api, LOCAL_USED_FILE, DRIVE_TARGET_FOLDER_ID, logger)

        os.remove(LOCAL_NEW_FILE)
        os.remove(LOCAL_USED_FILE)
        os.remove(LOG_FILENAME)
        logger.info("Local temporary files removed.")

    except Exception as e:
        logger.error(f"FATAL SCRIPT EXECUTION FAILURE: {e}")
        return f"Script Execution Failed: {e}", 500

    end_time = datetime.datetime.now()
    duration = end_time - start_time
    logger.info(f"--- Script Execution Finished ---\n")
    logger.info(f"Total Run Duration: {duration}")

    return f"Script ran successfully in {duration}. Log content: {final_log_content}", 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
