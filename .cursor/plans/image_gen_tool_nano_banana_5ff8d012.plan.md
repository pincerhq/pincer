---
name: Image Gen Tool Nano Banana
overview: "Implement the image_gen builtin tool with fal.ai Nano Banana (primary) and Gemini (fallback), following the spec while adapting to Pincer's current architecture: tools receive context and deliver images via channel.send_photo_from_bytes/send_photo during execution."
todos: []
isProject: false
---

# Image Generation Tool: Nano Banana + Gemini

## Architecture Adaptation

The spec assumes `ImageContent`, `pending_images`, and `channel.send_message(content)`. Pincer's current flow is simpler: **tools that need to send images receive `context` (user_id, channel, channel_map) and call `channel.send_photo_from_bytes()` or `channel.send_photo()` directly during execution.** The generate_image skill already does this. We will follow the same pattern for the builtin `image_gen` tool — no agent core changes, no MessageContent union, no pending_images.

---

## Phase 1: Image Provider Layer

### 1.1 Create `src/pincer/image/` module

**New files:**

- `src/pincer/image/__init__.py` — export public types
- `src/pincer/image/types.py` — `ImageRequest`, `ImageResult`, `GeneratedImage` (no `ImageContent` for channel — we use existing channel API)
- `src/pincer/image/provider_base.py` — `BaseImageProvider` ABC with `generate()`, `is_available()`, `estimate_cost()`
- `src/pincer/image/provider_fal.py` — `FalImageProvider` using `fal_client.run_async()`
- `src/pincer/image/provider_gemini.py` — `GeminiImageProvider` using `google.genai` with `response_modalities=[IMAGE]`
- `src/pincer/image/router.py` — `ImageProviderRouter` with auto-fallback (fal → gemini)

**Key types (simplified):**

```python
# types.py
@dataclass
class GeneratedImage:
    url: str | None = None      # fal returns URL
    bytes: bytes | None = None  # Gemini returns bytes
    content_type: str = "image/png"

@dataclass  
class ImageResult:
    images: list[GeneratedImage]
    provider: str
    model: str
    cost_usd: float
```

### 1.2 Config

**File:** [src/pincer/config.py](src/pincer/config.py)

Add fields (or nested `ImageGenConfig`):

- `image_provider: Literal["fal", "gemini", "auto"] = "auto"`
- `fal_key: SecretStr` (or `FAL_KEY` from env)
- `fal_model: str = "fal-ai/nano-banana-2"`
- `image_model_gemini: str = "gemini-2.5-flash-preview-image-generation"` (or `gemini-2.5-flash-image`)
- `image_max_cost_per_request: float = 0.50`
- `image_daily_limit: int = 50`

**Env vars:** `PINCER_IMAGE_PROVIDER`, `FAL_KEY`, reuse `PINCER_GEMINI_API_KEY` for Gemini.

---

## Phase 2: Builtin Tool

### 2.1 Create `src/pincer/tools/builtin/image_gen.py`

**Pattern:** Same as generate_image skill — async handler with `context` parameter. No `@tool` decorator; use `tools.register()`.

```python
async def image_gen(
    prompt: str,
    caption: str = "",
    aspect_ratio: str = "1:1",
    num_images: int = 1,
    context: dict | None = None,
) -> str:
    """Generate image via fal.ai or Gemini, send to user, return status."""
    ctx = context or {}
    channel_map = ctx.get("channel_map", {})
    user_id = ctx.get("user_id", "")
    ch_name = ctx.get("channel", "")

    # Resolve channel (same Discord thread logic as generate_image skill)
    channel = _resolve_channel(channel_map, ch_name)
    send_kwargs = _resolve_send_kwargs(ch_name, channel_map)

    # Route to provider
    router = get_image_router()  # singleton from config
    request = ImageRequest(prompt=prompt, num_images=num_images, ...)
    result = await router.generate(request)

    # Send each image via channel
    for img in result.images:
        if img.url:
            await channel.send_photo(user_id, img.url, caption, **send_kwargs)
        elif img.bytes:
            await channel.send_photo_from_bytes(
                user_id, img.bytes, img.content_type, caption, **send_kwargs
            )

    # Track cost
    await cost_tracker.add_image_cost(result.cost_usd, result.provider)

    return json.dumps({"status": "success", "provider": result.provider, ...})
```

**Router injection:** The tool needs the router and cost_tracker. Options:

- **A)** Pass via closure when registering (like `channel_map` for skills)
- **B)** Create router/cost_tracker in tool from `get_settings()` and a module-level cost_tracker ref

Recommend **A**: extend the registration pattern so builtin tools that need `context` also receive `channel_map`, `image_router`, `cost_tracker` via closure or an extended context.

### 2.2 Register in CLI

**File:** [src/pincer/cli.py](src/pincer/cli.py)

- Create `ImageProviderRouter` from settings (only if `FAL_KEY` or `PINCER_GEMINI_API_KEY` set)
- Register `image_gen` with `tools.register()` — **before** `python_exec` so it appears earlier
- Add exclusion to `python_exec` description: "Do NOT use for creating images from text — use image_gen for that."
- Update system prompt: "When the user asks to create, draw, or generate an image, use image_gen (not python_exec)."

---

## Phase 3: Cost Tracking

### 3.1 Extend CostTracker

**File:** [src/pincer/llm/cost_tracker.py](src/pincer/llm/cost_tracker.py)

- Add table or type column: `cost_log` already has `provider`, `model`; add `cost_type` = "llm" | "image" or a separate `image_cost_log` table
- `add_image_cost(cost_usd, provider)` — insert image cost
- `get_image_count_today()` — for daily limit check
- Include image costs in `get_summary()` and `get_today_spend()`

---

## Phase 4: Channel Support

**Existing:** All channels already implement `send_photo_from_bytes` and `send_photo`. No channel changes needed — the tool calls them directly.

**Voice:** Base `send_photo_from_bytes` sends text. No change.

---

## Phase 5: Migration

### 5.1 Deprecate generate_image skill

- Remove `skills/generate_image/` or add `env_required` that is never satisfied (e.g. `PINCER_IMAGE_LEGACY_SKILL_DISABLED`)
- Prefer: keep skill but add `"deprecated": true` in manifest and skip loading if `image_gen` tool is registered
- Simpler: delete the skill; `image_gen` builtin replaces it

### 5.2 Dependencies

**File:** [pyproject.toml](pyproject.toml)

```toml
dependencies = [
    ...
    "fal-client>=0.13.0",  # for image_gen (optional: make it conditional)
]
```

Or optional extra: `image = ["fal-client>=0.13.0"]` and only register tool when installed.

---

## Implementation Order


| Step | Task                                           | Files                      |
| ---- | ---------------------------------------------- | -------------------------- |
| 1    | Create `image/types.py`, `provider_base.py`    | NEW                        |
| 2    | Implement `provider_fal.py`                    | NEW                        |
| 3    | Implement `provider_gemini.py`                 | NEW                        |
| 4    | Implement `router.py`                          | NEW                        |
| 5    | Add ImageGen config to Settings                | config.py                  |
| 6    | Create `image_gen` builtin tool                | tools/builtin/image_gen.py |
| 7    | Extend CostTracker for images                  | cost_tracker.py            |
| 8    | Register image_gen in cli.py, inject router    | cli.py                     |
| 9    | Update python_exec description + system prompt | cli.py, config.py          |
| 10   | Remove/deprecate generate_image skill          | skills/generate_image      |
| 11   | Add fal-client to pyproject.toml               | pyproject.toml             |
| 12   | Doctor check for FAL_KEY / image config        | security/doctor.py         |


---

## Tool Naming

Spec uses `generate_image` and `edit_image`. Current skill uses `generate_image__generate_image`. To avoid collision:

- **Option A:** Builtin `image_gen` (different name)
- **Option B:** Builtin `generate_image` — remove skill, take the name

Recommend **B**: builtin `generate_image` replaces the skill. Simpler for users.

---

## Testing

- `test_provider_fal.py` — mock `fal_client.run_async`
- `test_provider_gemini.py` — mock `genai.Client`
- `test_router.py` — auto-fallback
- `test_image_gen_tool.py` — tool returns success, channel.send_photo_from_bytes called
- `test_cost_tracker_image.py` — image costs recorded

---

## Summary


| Spec Element                  | Adaptation                                         |
| ----------------------------- | -------------------------------------------------- |
| ImageContent, pending_images  | Not used — tool sends via channel during execution |
| channel.send_message(content) | Use existing send_photo / send_photo_from_bytes    |
| get_current_context()         | Use context dict injected by ToolRegistry          |
| @tool decorator               | Use tools.register() like other builtins           |
| edit_image tool               | Phase 2 optional — can add later                   |


