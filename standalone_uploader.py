"""Standalone Zenodo uploader without Prefect dependencies.

This module handles direct communication with the Zenodo API
for creating records and uploading files.
"""

import os
import requests
from requests.exceptions import HTTPError, RequestException
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass
import time


@dataclass
class Credentials:
    """Zenodo API credentials."""
    token: str
    base_url: str


def get_credentials_from_env() -> Credentials:
    """
    Load credentials from environment variables.
    
    Returns:
        Credentials object
        
    Raises:
        ValueError: If credentials are not set
    """
    token = os.getenv("INVENIO_RDM_ACCESS_TOKEN")
    base_url = os.getenv("INVENIO_RDM_BASE_URL")
    
    if not token or token == "ZENODO_ACESS_TOKEN":
        raise ValueError(
            "INVENIO_RDM_ACCESS_TOKEN not set or still using placeholder. "
            "Please update set_env.sh and run: source set_env.sh"
        )
    
    if not base_url:
        raise ValueError(
            "INVENIO_RDM_BASE_URL not set. "
            "Please update set_env.sh and run: source set_env.sh"
        )
    
    return Credentials(token=token, base_url=base_url)


def create_draft_record(credentials: Credentials, metadata: Dict[str, Any]) -> Dict[str, Any]:
    """
    Create a draft record on Zenodo.
    
    Args:
        credentials: Zenodo credentials
        metadata: Record metadata
        
    Returns:
        API response with draft record details
        
    Raises:
        HTTPError: If API request fails
    """
    url = f"{credentials.base_url}records"
    
    headers = {
        "Authorization": f"Bearer {credentials.token}",
        "Content-Type": "application/json"
    }
    
    response = requests.post(url, json=metadata, headers=headers)
    response.raise_for_status()
    
    return response.json()


def upload_file_to_draft(
    credentials: Credentials,
    record_id: str,
    file_path: str,
) -> Dict[str, Any]:
    """
    Upload a file to a draft record.
    
    Args:
        credentials: Zenodo credentials
        record_id: Draft record ID
        file_path: Path to file to upload
        
    Returns:
        API response with file details
        
    Raises:
        HTTPError: If upload fails
    """
    file_path_obj = Path(file_path)
    
    if not file_path_obj.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    
    url = f"{credentials.base_url}records/{record_id}/draft/files"
    
    headers = {
        "Authorization": f"Bearer {credentials.token}",
    }
    
    # Step 1: Initialize file upload
    init_data = [{"key": file_path_obj.name}]
    response = requests.post(url, json=init_data, headers=headers)
    response.raise_for_status()
    
    entries = response.json().get("entries", [])
    if not entries:
        raise ValueError(f"Failed to initialize upload for {file_path_obj.name}")
    
    # Step 2: Upload file content
    file_entry = entries[0]
    upload_url = file_entry["links"]["content"]
    commit_url = file_entry["links"]["commit"]
    
    with open(file_path, "rb") as f:
        upload_response = requests.put(
            upload_url,
            data=f,
            headers={"Authorization": f"Bearer {credentials.token}"}
        )
        upload_response.raise_for_status()
    
    # Step 3: Commit the file
    commit_response = requests.post(
        commit_url,
        headers={"Authorization": f"Bearer {credentials.token}"}
    )
    commit_response.raise_for_status()
    
    return commit_response.json()


def publish_draft(credentials: Credentials, record_id: str) -> Dict[str, Any]:
    """
    Publish a draft record.
    
    Args:
        credentials: Zenodo credentials
        record_id: Draft record ID
        
    Returns:
        API response with published record details
        
    Raises:
        HTTPError: If publish fails
    """
    url = f"{credentials.base_url}records/{record_id}/draft/actions/publish"
    
    headers = {
        "Authorization": f"Bearer {credentials.token}",
    }
    
    response = requests.post(url, headers=headers)
    response.raise_for_status()
    
    return response.json()


def delete_draft(credentials: Credentials, record_id: str) -> None:
    """
    Delete a draft record.
    
    Args:
        credentials: Zenodo credentials
        record_id: Draft record ID
        
    Raises:
        HTTPError: If delete fails
    """
    url = f"{credentials.base_url}records/{record_id}/draft"
    
    headers = {
        "Authorization": f"Bearer {credentials.token}",
    }
    
    response = requests.delete(url, headers=headers)
    response.raise_for_status()


async def upload_to_zenodo(
    files: List[str],
    config: Any,  # DraftConfig
    delete_on_failure: bool = False,
    auto_publish: bool = False,
) -> Dict[str, Any]:
    """
    Upload files to Zenodo.
    
    Args:
        files: List of file paths to upload
        config: Draft configuration (DraftConfig object)
        delete_on_failure: Delete record if upload fails
        auto_publish: Automatically publish after upload
        
    Returns:
        Dictionary with upload result:
        {
            'successful': bool,
            'api_response': dict or None,
            'error': dict or None
        }
    """
    credentials = get_credentials_from_env()
    record_id = None
    
    try:
        # Create draft record
        print(f"Creating draft record...")
        
        # Handle record_access and files_access - they might be enums or strings
        record_access = config.record_access.value if hasattr(config.record_access, 'value') else config.record_access
        files_access = config.files_access.value if hasattr(config.files_access, 'value') else config.files_access
        
        draft_metadata = {
            "access": {
                "record": record_access,
                "files": files_access,
            },
            "files": {
                "enabled": config.files_enabled,
            },
            "metadata": config.metadata,
        }
        
        # Add community if specified
        if config.community_id:
            draft_metadata["parent"] = {
                "communities": {
                    "ids": [config.community_id]
                }
            }
        
        # Add custom fields if specified
        if config.custom_fields:
            draft_metadata["custom_fields"] = config.custom_fields
        
        draft_response = create_draft_record(credentials, draft_metadata)
        record_id = draft_response.get("id")
        
        if not record_id:
            raise ValueError("No record ID returned from draft creation")
        
        print(f"✅ Draft created with ID: {record_id}")
        
        # Upload files
        print(f"Uploading {len(files)} file(s)...")
        for i, file_path in enumerate(files, 1):
            file_name = Path(file_path).name
            file_size_mb = Path(file_path).stat().st_size / (1024 * 1024)
            
            print(f"  [{i}/{len(files)}] Uploading {file_name} ({file_size_mb:.2f} MB)...")
            
            start_time = time.time()
            upload_file_to_draft(credentials, record_id, file_path)
            elapsed = time.time() - start_time
            
            print(f"  ✅ Uploaded in {elapsed:.1f}s")
        
        print(f"✅ All files uploaded successfully")
        
        # Publish if requested
        if auto_publish:
            print(f"Publishing record...")
            publish_response = publish_draft(credentials, record_id)
            print(f"✅ Record published")
            
            return {
                'successful': True,
                'api_response': publish_response,
                'error': None
            }
        else:
            print(f"✅ Record created as draft (not published)")
            
            return {
                'successful': True,
                'api_response': draft_response,
                'error': None
            }
    
    except HTTPError as e:
        error_msg = f"HTTP error: {e}"
        
        # Try to get error details from response
        try:
            error_details = e.response.json()
            error_msg = f"HTTP {e.response.status_code}: {error_details}"
        except:
            pass
        
        print(f"❌ Upload failed: {error_msg}")
        
        # Delete draft if requested
        if delete_on_failure and record_id:
            try:
                print(f"Deleting failed draft {record_id}...")
                delete_draft(credentials, record_id)
                print(f"✅ Draft deleted")
            except Exception as del_e:
                print(f"⚠️  Failed to delete draft: {del_e}")
        
        return {
            'successful': False,
            'api_response': None,
            'error': {
                'type': 'HTTPError',
                'error_message': error_msg
            }
        }
    
    except RequestException as e:
        error_msg = f"Connection error: {e}"
        print(f"❌ Upload failed: {error_msg}")
        
        # Delete draft if requested
        if delete_on_failure and record_id:
            try:
                print(f"Deleting failed draft {record_id}...")
                delete_draft(credentials, record_id)
                print(f"✅ Draft deleted")
            except Exception as del_e:
                print(f"⚠️  Failed to delete draft: {del_e}")
        
        return {
            'successful': False,
            'api_response': None,
            'error': {
                'type': 'RequestException',
                'error_message': error_msg
            }
        }
    
    except Exception as e:
        error_msg = str(e)
        print(f"❌ Upload failed: {error_msg}")
        
        # Delete draft if requested
        if delete_on_failure and record_id:
            try:
                print(f"Deleting failed draft {record_id}...")
                delete_draft(credentials, record_id)
                print(f"✅ Draft deleted")
            except Exception as del_e:
                print(f"⚠️  Failed to delete draft: {del_e}")
        
        return {
            'successful': False,
            'api_response': None,
            'error': {
                'type': type(e).__name__,
                'error_message': error_msg
            }
        }
