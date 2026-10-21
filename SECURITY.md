# Security policy

This project reads local coding-session transcripts and exposes a local dashboard
with APIs that can change local configuration. Session content, message-bus files,
paths and imported assets may contain private data. Keep the board on loopback;
do not expose it to the public internet or treat it as an authenticated remote
service.

## Report a vulnerability privately

If enabled for this repository, use GitHub's
[private vulnerability reporting](https://github.com/another-bbzj/claude-agent-team/security/advisories/new).
If that option is unavailable, use a contact method listed on the
[maintainer's profile](https://github.com/another-bbzj) to request a private channel.
Do not publish exploit details or private transcripts in an ordinary issue while
arranging that channel. No dedicated security mailbox or response deadline is
promised here.

Include the affected version or commit, operating system, impact, and a minimal
reproduction with synthetic data. Explain any required conditions, such as a
browser origin, local file, archive or configuration. Remove credentials, session
text, private paths and personally identifying information before sharing files.

For a non-sensitive reliability bug, use the
[bug report form](https://github.com/another-bbzj/claude-agent-team/issues/new?template=bug_report.yml).

## Scope and updates

Reports about unsafe local file access, cross-origin API requests, private-data
exposure, imported files and install/update behavior are relevant. Test only on
systems and data you control. The repository currently has no published schedule
for security backports or a separately maintained release line; identify the
exact commit in reports and check current `main` when validating a fix.

中文：安全问题请优先使用 GitHub 私密漏洞报告；若未启用，请通过维护者主页
提供的联系方式先确认私密渠道。公开 issue 请勿包含利用细节、真实转录或密钥。
