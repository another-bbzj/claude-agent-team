# Textfile refresh examples

这些单元和 cron 行是示例，不会被 `install.py` 安装。仓库不安装 cron、systemd、node_exporter 或 Grafana。把路径换成你的检出目录和采集目录；目录必须已经存在。采集账号要能访问看板上的 `127.0.0.1`，并且能写 node_exporter 的 textfile 目录。失败时不要删掉上一份成功的 `.prom` 文件。细节和隐私边界见 [docs/INTEGRATIONS.md](../../docs/INTEGRATIONS.md)。

The commands below call the existing exporter. They do not add a scheduler to this repository. Replace both paths. The output directory must already exist, and the account that runs the command must be able to read the loopback board and write the textfile directory. A failed run leaves the previous file in place.

Omit `--session` and `--project` to keep the board's default session. Add one of them, not both, when you need a different session. See the Prometheus section of the integration guide.

## cron

```cron
* * * * * cd /opt/claude-agent-team && python tools/export_snapshot.py --port 7788 --format prometheus --output /var/lib/node_exporter/textfile_collector/claude_agent_team.prom
```

## systemd

`claude-agent-team-textfile.service`:

```ini
[Unit]
Description=Write Claude Agent Team Prometheus textfile

[Service]
Type=oneshot
WorkingDirectory=/opt/claude-agent-team
ExecStart=/usr/bin/python tools/export_snapshot.py --port 7788 --format prometheus --output /var/lib/node_exporter/textfile_collector/claude_agent_team.prom
```

`claude-agent-team-textfile.timer`:

```ini
[Unit]
Description=Refresh Claude Agent Team textfile every minute

[Timer]
OnBootSec=30s
OnUnitActiveSec=1min
AccuracySec=10s
Persistent=true

[Install]
WantedBy=timers.target
```

Enable the timer with `systemctl enable --now claude-agent-team-textfile.timer` only after the unit files are installed on that host. Prometheus scrapes node_exporter, not the board's snapshot port. Match the textfile name if you also use the freshness expressions in the integration guide (`claude_agent_team.prom`).
