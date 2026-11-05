# 静态合成演示 / Static synthetic demo

用浏览器打开旁边的 [`index.html`](index.html)。页面嵌着 `examples/board-demo.synthetic.json` 经只读演示算好的快照，不启动 Python，也不读本机转录。时钟停在这份快照的 `generatedAt`，所以「演示文档」一直是停了 15 分钟。地址栏加 `?budget=over` 会显示红色预算护栏。点作战区里的另一条分支，只改分支标签。

Open [`index.html`](index.html) next to this file. The embedded snapshot comes from `examples/board-demo.synthetic.json`. Nothing is uploaded. The clock stays on that snapshot's `generatedAt`. `?budget=over` shows the red budget guard.

GitHub Pages 地址 / published URL: <https://another-bbzj.github.io/claude-agent-team/>

若这个地址是 404，维护者需要做一次：Settings → Pages → Build and deployment → Source 选 **GitHub Actions**，然后重跑 `pages` workflow。仓库本身不会改这个设置。没打开之前，本地文件照样能看。

If that URL 404s, set Settings → Pages → Build and deployment → Source to **GitHub Actions** once, then re-run the `pages` workflow. The repository does not flip that setting. The local files work either way.

重新生成 / rebuild from the repository root:

```sh
python tools/build_pages_site.py
python tools/build_pages_site.py --check
```

形象文件是仓库里 MIT 的 `aurelia-engineer.png` 和 `lyra-designer.png`，从 `team-board/sprites/` 原样复制。快照文本由 `tests/test_pages_site.py` 用和 `examples/` 相同的隐私正则扫描。
