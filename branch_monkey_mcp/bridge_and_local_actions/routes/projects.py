"""
Project folder management endpoints for the local server.

Handles creating project folders with auto-numbering (p_X_name format),
scanning folders for configuration, and browsing the file system.
"""

import os
import re
import subprocess
import json
from typing import Optional, List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config import get_home_directory

router = APIRouter()


class CreateProjectFolderRequest(BaseModel):
    """Request to create a new project folder."""
    base_path: str  # e.g., ~/Code
    project_name: str  # e.g., my-saas-app
    init_git: bool = True  # Initialize git repo


class ScanProjectRequest(BaseModel):
    """Request to scan a folder for project configuration."""
    path: str


class ListFoldersRequest(BaseModel):
    """Request to list folders in a directory."""
    path: str


def expand_path(path: str) -> str:
    """Expand ~ and environment variables in path."""
    return os.path.expanduser(os.path.expandvars(path))


def sanitize_project_name(name: str) -> str:
    """
    Sanitize project name for folder creation.
    Converts to lowercase, replaces spaces with hyphens, removes special chars.
    """
    # Convert to lowercase
    name = name.lower()
    # Replace spaces and underscores with hyphens
    name = re.sub(r'[\s_]+', '-', name)
    # Remove any characters that aren't alphanumeric or hyphens
    name = re.sub(r'[^a-z0-9-]', '', name)
    # Remove consecutive hyphens
    name = re.sub(r'-+', '-', name)
    # Remove leading/trailing hyphens
    name = name.strip('-')
    return name


@router.post("/create-project-folder")
def create_project_folder(request: CreateProjectFolderRequest):
    """
    Create a new project folder.

    Returns:
        {
            path: Full path to created folder,
            folder_name: Just the folder name,
            git_initialized: Whether git was initialized
        }
    """
    base_path = expand_path(request.base_path)

    # Validate base path exists
    if not os.path.isdir(base_path):
        raise HTTPException(
            status_code=400,
            detail=f"Base path does not exist: {base_path}"
        )

    # Sanitize project name
    folder_name = sanitize_project_name(request.project_name)
    if not folder_name:
        raise HTTPException(
            status_code=400,
            detail="Project name results in empty folder name after sanitization"
        )

    full_path = os.path.join(base_path, folder_name)

    # Check if folder already exists
    if os.path.exists(full_path):
        raise HTTPException(
            status_code=409,
            detail=f"Folder already exists: {folder_name}"
        )

    try:
        # Create the folder
        os.makedirs(full_path)

        git_initialized = False

        # Initialize git if requested
        if request.init_git:
            try:
                subprocess.run(
                    ["git", "init"],
                    cwd=full_path,
                    capture_output=True,
                    check=True
                )
                git_initialized = True
            except subprocess.CalledProcessError:
                # Git init failed, but folder was created
                pass

        return {
            "path": full_path,
            "folder_name": folder_name,
            "git_initialized": git_initialized
        }

    except PermissionError:
        raise HTTPException(
            status_code=403,
            detail=f"Permission denied creating folder: {full_path}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create folder: {str(e)}"
        )


@router.post("/scan-project")
def scan_project(request: ScanProjectRequest):
    """
    Scan a folder for project configuration.

    Detects:
    - Git remote URL from .git/config
    - Framework from package.json
    - Deployment platform from config files (wrangler.toml, vercel.json, etc.)
    - Dev server command and port from package.json

    Returns:
        {
            git_remote: URL of git remote (if any),
            framework: Detected framework name,
            deployment_platform: Detected deployment platform,
            dev_server: { command, port } if detected,
            raw_config: Object with detected config file contents
        }
    """
    path = expand_path(request.path)

    if not os.path.isdir(path):
        raise HTTPException(
            status_code=400,
            detail=f"Directory does not exist: {path}"
        )

    result = {
        "git_remote": None,
        "framework": None,
        "deployment_platform": None,
        "dev_server": None,
        "raw_config": {}
    }

    # Detect git remote
    git_config_path = os.path.join(path, ".git", "config")
    if os.path.exists(git_config_path):
        try:
            git_result = subprocess.run(
                ["git", "config", "--get", "remote.origin.url"],
                cwd=path,
                capture_output=True,
                text=True
            )
            if git_result.returncode == 0:
                result["git_remote"] = git_result.stdout.strip()
        except Exception:
            pass

    # Detect framework and dev server from package.json
    package_json_path = os.path.join(path, "package.json")
    if os.path.exists(package_json_path):
        try:
            with open(package_json_path, 'r') as f:
                package_json = json.load(f)
                result["raw_config"]["package_json"] = package_json

                # Detect framework from dependencies
                deps = {
                    **package_json.get("dependencies", {}),
                    **package_json.get("devDependencies", {})
                }

                if "@sveltejs/kit" in deps:
                    result["framework"] = "SvelteKit"
                elif "next" in deps:
                    result["framework"] = "Next.js"
                elif "nuxt" in deps:
                    result["framework"] = "Nuxt"
                elif "astro" in deps:
                    result["framework"] = "Astro"
                elif "gatsby" in deps:
                    result["framework"] = "Gatsby"
                elif "svelte" in deps:
                    result["framework"] = "Svelte"
                elif "react" in deps:
                    result["framework"] = "React"
                elif "vue" in deps:
                    result["framework"] = "Vue"
                elif "express" in deps:
                    result["framework"] = "Express"
                elif "fastify" in deps:
                    result["framework"] = "Fastify"

                # Detect dev server
                scripts = package_json.get("scripts", {})
                dev_command = scripts.get("dev") or scripts.get("start")
                if dev_command:
                    # Try to detect port from command
                    port_match = re.search(r'(?:--port|PORT=|:)(\d{4,5})', dev_command)
                    port = int(port_match.group(1)) if port_match else 3000

                    # Adjust default port based on framework
                    if not port_match:
                        if result["framework"] == "SvelteKit":
                            port = 5173
                        elif result["framework"] in ["Next.js", "Nuxt", "Gatsby"]:
                            port = 3000
                        elif result["framework"] == "Astro":
                            port = 4321

                    result["dev_server"] = {
                        "command": "dev" if "dev" in scripts else "start",
                        "port": port
                    }
        except Exception:
            pass

    # Detect deployment platform from config files
    deployment_configs = [
        ("wrangler.toml", "Cloudflare Pages"),
        ("wrangler.json", "Cloudflare Pages"),
        ("vercel.json", "Vercel"),
        ("netlify.toml", "Netlify"),
        ("railway.json", "Railway"),
        ("railway.toml", "Railway"),
        ("fly.toml", "Fly.io"),
        ("render.yaml", "Render"),
    ]

    for config_file, platform in deployment_configs:
        config_path = os.path.join(path, config_file)
        if os.path.exists(config_path):
            result["deployment_platform"] = platform
            try:
                with open(config_path, 'r') as f:
                    content = f.read()
                    # Store first 2000 chars of config for reference
                    result["raw_config"][config_file] = content[:2000]
            except Exception:
                pass
            break  # Use first match

    return result


@router.post("/list-folders")
def list_folders(request: ListFoldersRequest):
    """
    List folders in a directory for the folder browser.

    Returns:
        {
            path: The requested path (expanded),
            parent: Parent directory path,
            folders: List of { name, path, is_git_repo }
        }
    """
    path = expand_path(request.path)

    if not os.path.isdir(path):
        raise HTTPException(
            status_code=400,
            detail=f"Directory does not exist: {path}"
        )

    folders = []

    try:
        entries = sorted(os.listdir(path))
        for entry in entries:
            full_path = os.path.join(path, entry)
            if os.path.isdir(full_path) and not entry.startswith('.'):
                folders.append({
                    "name": entry,
                    "path": full_path,
                    "is_git_repo": os.path.exists(os.path.join(full_path, ".git"))
                })
    except PermissionError:
        raise HTTPException(
            status_code=403,
            detail=f"Permission denied reading directory: {path}"
        )

    return {
        "path": path,
        "parent": os.path.dirname(path),
        "folders": folders
    }


@router.get("/home-directory")
def get_home_dir():
    """
    Get the home directory configured for this relay.

    Returns:
        {
            home_directory: The configured home directory,
            default_code_path: Suggested default code path (~/Code)
        }
    """
    home_dir = get_home_directory()
    default_code = expand_path("~/Code")

    return {
        "home_directory": home_dir,
        "default_code_path": default_code if os.path.isdir(default_code) else home_dir
    }
