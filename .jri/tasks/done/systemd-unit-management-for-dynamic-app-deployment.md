---
title: Systemd unit management for dynamic app deployment
priority: 0
assignee: Nicolás Pujia
depends_on:
- add-deployment-columns-to-projects-table-and-port-allocation
created: '2026-03-21'
acceptance_criteria:
- app/deploy_manager.py exists with all 6 functions
- generate_systemd_unit returns valid unit file content
- deploy_dynamic writes unit file, reloads daemon, starts service
- deploy_static creates symlink to detected static output dir
- stop_deploy stops the service/removes symlink
- get_deploy_logs returns journalctl output
- All subprocess calls are async with timeouts
---

Create helper functions that generate and manage systemd user units for deployed dynamic web apps.

WHAT TO CREATE in app/deploy_manager.py (new file):

1. Function generate_systemd_unit(project_name, project_dir, start_command, port, user='nico') -> str:
   Returns the content of a systemd unit file. Template:
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

   The start_command should be wrapped so the app binds to the allocated port. Set PORT and HOST env vars so apps can read them.

2. Function deploy_dynamic(project_id, project_name, project_dir, start_command, port) -> None:
   - Write the unit file to /etc/systemd/system/jri-{project_name}.service
   - Run: systemctl daemon-reload
   - Run: systemctl enable jri-{project_name}
   - Run: systemctl start jri-{project_name}
   - Update DB: deploy_status = 'running'
   All commands via asyncio.create_subprocess_exec with sudo.

3. Function deploy_static(project_name, project_dir) -> None:
   - Detect the static output directory. Check in order: dist/, build/, public/, out/, .output/public/, the project dir itself
   - Create/update a symlink: /var/www/jri-sites/{project_name} -> {detected_dir}
   - Update DB: deploy_status = 'running'

4. Function stop_deploy(project_name, deploy_type) -> None:
   - For dynamic: systemctl stop jri-{project_name}
   - For static: remove the symlink
   - Update DB: deploy_status = 'stopped'

5. Function restart_deploy(project_name) -> None:
   - systemctl restart jri-{project_name}

6. Function get_deploy_logs(project_name, lines=50) -> str:
   - journalctl -u jri-{project_name} -n {lines} --no-pager
   - Return stdout as string

All subprocess calls should use asyncio and have 15s timeouts.
