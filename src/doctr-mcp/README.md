# 📄 docTR MCP Server

A [Model Context Protocol](https://modelcontextprotocol.io) server exposing OCR
(optical character recognition) built on [mindee/doctr](https://github.com/mindee/doctr),
using [FastMCP](https://github.com/jlowin/fastmcp). Detects and recognizes text in
images and PDFs, with full block/line/word geometry and key-information extraction
(KIE). (Layout detection and table-structure extraction are not available in the
pinned `python-doctr==1.0.1` release — those exist only on doctr's unreleased
GitHub `main` branch.)

## Tools

| Tool | Description |
|------|-------------|
| `ocr_document_text` | Quick plain-text read of an image or PDF — no geometry, the cheap default for "just read this" |
| `ocr_document` | Full end-to-end OCR: page → block → line → word tree with geometry, confidence, orientation, and language |
| `detect_text` | Text detection only — boxes/polygons, no recognition |
| `recognize_text` | Text recognition only, on a batch of pre-cropped word/line images |
| `extract_key_info` | Key-information extraction (KIE) — predictions grouped by detected class |
| `list_architectures` | Discover valid `det_arch`/`reco_arch` values |

These are the raw names this server reports over MCP. A connecting client may
additionally namespace them — e.g. Pincer's own bridge prefixes every tool with its
configured `pincer.toml` server name, so they appear there as `doctr__ocr_document`,
`doctr__detect_text`, etc. (see `docs/guides/mcp-guide.md`'s "Tool naming" section).

All document-input tools accept either `image_b64` (base64-encoded image or PDF
bytes — the portable default, required over the HTTP/Docker deployment) or
`file_path` (an absolute path, only usable when the server runs via stdio with a
filesystem shared with the caller) — exactly one of the two per call.

## Quick Start

### With Docker Compose (recommended)

```bash
docker compose up -d
```

The server is available at `http://localhost:8000/mcp`. First-time build downloads
torch and a curated set of pre-baked model weights (see [Notes](#notes) on image
size) — subsequent starts are fast since weights are cached in the
`doctr-model-cache` volume.

### Without Docker

```bash
uv sync
uv run doctr-mcp-run --transport http
```

Or over stdio (used by Pincer's own `pincer.toml` example):

```bash
uv run doctr-mcp-run --transport stdio
```

### Custom port

```bash
MCP_PORT=9000 docker compose up -d
```

## Connect to Claude Code

```bash
claude mcp add doctr --transport http http://localhost:8000/mcp
```

## Connect to Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "doctr": {
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

## Environment variables

See [docs/core-components/mcp-servers.md](../../docs/core-components/mcp-servers.md)
for the full reference table shared across all bundled servers. doctr-mcp-specific
variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `DOCTR_DEVICE` | `cpu` | `cpu` or `cuda` — never auto-detected |
| `DOCTR_MAX_WORKERS` | `1` | Max concurrent doctr inferences process-wide |
| `DOCTR_TORCH_THREADS` | `0` (= `os.cpu_count()`) | Passed to `torch.set_num_threads()` at startup |
| `DOCTR_MAX_CACHED_PREDICTORS` | `4` | Bounded LRU size for the predictor cache |
| `DOCTR_MAX_INPUT_MB` | `20` | Reject decoded image/PDF payloads larger than this |
| `DOCTR_DEFAULT_DET_ARCH` | `db_resnet50` | Default detection architecture when a tool call omits `det_arch` |
| `DOCTR_DEFAULT_RECO_ARCH` | `crnn_vgg16_bn` | Default recognition architecture when a tool call omits `reco_arch` |
| `DOCTR_CACHE_DIR` | `~/.cache/doctr` | Model weight cache directory (read natively by docTR itself) |

## Notes

- **Architecture names are not restricted** to a fixed enum — `det_arch`/`reco_arch`
  accept any string docTR itself supports (call
  `list_architectures` to discover known values). The Docker image pre-bakes
  a curated set of lightweight architectures at build time (`db_resnet50`,
  `db_mobilenet_v3_large`, `fast_base` for detection; `crnn_vgg16_bn`,
  `crnn_mobilenet_v3_small` for recognition) so switching the `DOCTR_DEFAULT_*_ARCH`
  env vars between them doesn't trigger a cold download — override the baked set
  with `--build-arg PREBAKE_DET_ARCHS="..."` / `PREBAKE_RECO_ARCHS="..."`.
- **CPU-only by default.** No GPU passthrough is configured in this repo's Docker
  setup; set `DOCTR_DEVICE=cuda` only if you know your deployment has one.
- **This is the heaviest bundled MCP server** — the image includes torch,
  torchvision, and baked model weights, and is expected to be in the 1.5–3GB range
  (vs. tens-to-hundreds of MB for `sqlite-vec-memory-mcp`'s ONNX-based image). Build
  and first pull will take noticeably longer than the other bundled servers.
- Predictor construction is **lazy** (only on first tool call, not at server
  startup) and cached per exact parameter set (architectures + thresholds), bounded
  by `DOCTR_MAX_CACHED_PREDICTORS` with LRU eviction.
- Multi-page PDFs can be sliced with the optional `page_range` parameter (e.g.
  `[0, 3]` for the first three pages) to bound response size.
