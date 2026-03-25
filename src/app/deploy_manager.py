"""Systemd unit management for dynamic and static app deployment."""

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

UNIT_DIR = "/etc/systemd/system"
SITES_DIR = "/var/www/jri-sites"
SUBPROCESS_TIMEOUT = 15


def generate_systemd_unit(
    project_name: str, project_dir: str, start_command: str, port: int
) -> str:
    """Return systemd unit file content for a dynamic deploy."""
    return f"""\
[Unit]
Description=JRI deploy: {project_name}
After=network.target

[Service]
Type=simple
WorkingDirectory={project_dir}
ExecStart=/bin/bash -c '{start_command}'
Environment=PORT={port}
Environment=HOST=127.0.0.1
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


async def _run(*args: str) -> str:
    """Run a subprocess with a timeout and return its stdout."""
    logger.debug("Running: %s", " ".join(args))
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=SUBPROCESS_TIMEOUT
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise

    if proc.returncode != 0:
        raise RuntimeError(
            f"Command {args} failed (rc={proc.returncode}): {stderr.decode().strip()}"
        )
    return stdout.decode()


async def deploy_dynamic(
    project_name: str, project_dir: str, start_command: str, port: int
) -> None:
    """Write a systemd unit, enable, and start a dynamic deploy."""
    service = f"jri-deploy-{project_name}"
    unit_path = f"{UNIT_DIR}/{service}.service"
    unit_content = generate_systemd_unit(
        project_name, project_dir, start_command, port
    )

    logger.info("Deploying dynamic service %s -> %s", service, unit_path)

    # Write unit file via sudo tee
    proc = await asyncio.create_subprocess_exec(
        "sudo", "tee", unit_path,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        await asyncio.wait_for(
            proc.communicate(input=unit_content.encode()),
            timeout=SUBPROCESS_TIMEOUT,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise

    await _run("sudo", "systemctl", "daemon-reload")
    await _run("sudo", "systemctl", "enable", service)
    await _run("sudo", "systemctl", "start", service)

    logger.info("Dynamic service %s started", service)


async def deploy_static(project_name: str, project_dir: str) -> str:
    """Detect a static output directory, symlink it under /var/www/jri-sites/."""
    candidates = ["dist", "build", "public", "out", ".output/public"]
    detected = project_dir

    for candidate in candidates:
        path = Path(project_dir) / candidate
        if path.is_dir():
            detected = str(path)
            break

    logger.info(
        "Deploying static site %s: %s -> %s/%s",
        project_name, detected, SITES_DIR, project_name,
    )

    await _run("sudo", "mkdir", "-p", SITES_DIR)
    await _run(
        "sudo", "ln", "-sf", detected, f"{SITES_DIR}/{project_name}"
    )

    logger.info("Static site %s linked", project_name)
    return detected


async def stop_deploy(project_name: str, deploy_type: str) -> None:
    """Stop a dynamic service or remove a static site symlink."""
    if deploy_type == "dynamic":
        service = f"jri-deploy-{project_name}"
        logger.info("Stopping dynamic service %s", service)
        await _run("sudo", "systemctl", "stop", service)
    elif deploy_type == "static":
        link = f"{SITES_DIR}/{project_name}"
        logger.info("Removing static site link %s", link)
        await _run("sudo", "rm", "-f", link)
    else:
        raise ValueError(f"Unknown deploy_type: {deploy_type}")


async def restart_deploy(project_name: str) -> None:
    """Restart a dynamic deploy service."""
    service = f"jri-deploy-{project_name}"
    logger.info("Restarting service %s", service)
    await _run("sudo", "systemctl", "restart", service)


async def get_deploy_logs(project_name: str, lines: int = 50) -> str:
    """Return recent journal logs for a deploy service."""
    service = f"jri-deploy-{project_name}"
    logger.info("Fetching %d log lines for %s", lines, service)
    return await _run(
        "journalctl", "-u", service, "-n", str(lines), "--no-pager"
    )
