# Import local wallpaper

Open **Appearance / 外观 → Upload / 上传**. Choose or drop a PNG, WebP, JPEG or
GIF (up to 15 MiB), or an MP4/WebM video (up to 50 MiB). Videos loop muted and
have a pause control. Browser codec support determines playback; files are not
transcoded. A valid container signature is not a guarantee that a browser can
decode every codec inside that container.

The local-path field also accepts a media file or a Wallpaper Engine project
directory containing `project.json`. For example, enter the project's directory
from a Steam workshop library; the directory need not be discovered by the
Steam scanner first.

- Image/video projects import their declared local media.
- Scene/web projects import a local preview only. The board does not run a 3D
  renderer, HTML wallpaper, JavaScript or executables from the project.
- Project references must stay within that directory. Remote URLs, network
  paths, traversal and symbolic links/reparse points are rejected.

Steam-discovered wallpaper selection and current-desktop capture remain
available. The existing Steam scene-image extraction behavior is independent of
the direct-folder preview fallback; the import result explains which is used.

Imported bytes are stored with content-based names under local
`team-board/backdrops/`; they never enter the repository. The active configuration
is updated only after a valid import. An invalid path, format or combined
setting leaves the current configuration intact. Imported videos support HTTP
Range/HEAD for native playback. Closing/deleting the active background removes
its imported file; switching backgrounds can leave older content-addressed
files locally, so this is not a full media-library cleanup tool.

Images/wallpapers retain their authors' rights. Use your own files or files you
are entitled to use; the project redistributes no third-party wallpaper assets.

The API remains `POST /api/backdrop` with `{path: ...}` or
`{data: base64-or-data-URL, name: ...}`. Only this endpoint permits a 72 MiB JSON
request body to accommodate a base64-encoded 50 MiB video; other mutation limits
are unchanged. The response includes an explanatory `note` for previews/codecs.
