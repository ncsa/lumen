// Builds OpenCode / curl / Python connection snippets for the /connect page.
// Reads server data from #connect-data; the OpenCode config always covers every
// accessible model, while the curl/Python examples follow the model selector and
// adapt to the selected model's modalities (image / audio).
(function () {
  const dataEl = document.getElementById("connect-data");
  if (!dataEl) return;
  const data = JSON.parse(dataEl.textContent);
  const BASE = data.base_url;
  const PLACEHOLDER = "MODEL";

  const select = document.getElementById("connect-model");

  function modelById(id) {
    return data.models.find((m) => m.id === id) || null;
  }

  // ── snippet block factory ────────────────────────────────────────────────
  function block(title, code, extraButtons, note) {
    const wrap = document.createElement("div");
    wrap.className = "snippet mb-3";

    const head = document.createElement("div");
    head.className = "d-flex justify-content-between align-items-center mb-1 gap-2";
    const label = document.createElement("span");
    label.className = "fw-semibold";
    label.textContent = title;
    head.appendChild(label);

    const btns = document.createElement("div");
    btns.className = "d-flex gap-2";
    (extraButtons || []).forEach((b) => btns.appendChild(b));

    const copyBtn = document.createElement("button");
    copyBtn.type = "button";
    copyBtn.className = "btn btn-sm btn-outline-secondary";
    copyBtn.textContent = "Copy";
    copyBtn.setAttribute("aria-label", "Copy " + title + " to clipboard");
    copyBtn.addEventListener("click", () => {
      navigator.clipboard.writeText(code).then(() => {
        copyBtn.textContent = "Copied!";
        setTimeout(() => { copyBtn.textContent = "Copy"; }, 2000);
      });
    });
    btns.appendChild(copyBtn);
    head.appendChild(btns);

    const pre = document.createElement("pre");
    pre.className = "border rounded p-2 bg-light mb-0";
    pre.style.overflow = "auto";
    const codeEl = document.createElement("code");
    codeEl.textContent = code;
    pre.appendChild(codeEl);

    wrap.appendChild(head);
    wrap.appendChild(pre);
    if (note) {
      const n = document.createElement("p");
      n.className = "text-muted small mt-1 mb-0";
      n.textContent = note;
      wrap.appendChild(n);
    }
    return wrap;
  }

  // ── OpenCode (always every accessible model) ─────────────────────────────
  function openCodeConfig() {
    const models = {};
    if (data.models.length) {
      data.models.forEach((m) => {
        const entry = { name: m.name + " via " + data.provider_name };
        if (m.context_window) {
          entry.limit = {
            context: m.context_window,
            output: m.max_output_tokens || m.context_window,
          };
        }
        // Costs are USD per million tokens — OpenCode's models.dev cost unit.
        entry.cost = { input: m.input_cost_per_million, output: m.output_cost_per_million };
        models[m.id] = entry;
      });
    } else {
      models[PLACEHOLDER] = { name: "Model via " + data.provider_name };
    }
    return {
      "$schema": "https://opencode.ai/config.json",
      provider: {
        [data.provider]: {
          npm: "@ai-sdk/openai-compatible",
          name: data.provider_name,
          options: { baseURL: BASE, apiKey: "{env:LUMEN_API_KEY}" },
          models: models,
        },
      },
    };
  }

  function renderOpenCode() {
    const container = document.getElementById("examples-opencode");
    container.innerHTML = "";
    const json = JSON.stringify(openCodeConfig(), null, 2);

    const download = document.createElement("button");
    download.type = "button";
    download.className = "btn btn-sm btn-primary";
    download.textContent = "Download config.json";
    download.setAttribute("aria-label", "Download opencode.json");
    download.addEventListener("click", () => {
      const blob = new Blob([json], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "opencode.json";
      a.click();
      URL.revokeObjectURL(url);
    });

    container.appendChild(block("opencode.json", json, [download]));
  }

  // ── curl ─────────────────────────────────────────────────────────────────
  function curlText(id) {
    return "curl " + BASE + "/chat/completions \\\n" +
      '  -H "Authorization: Bearer $LUMEN_API_KEY" \\\n' +
      '  -H "Content-Type: application/json" \\\n' +
      "  -d '{\n" +
      '    "model": "' + id + '",\n' +
      '    "messages": [{"role": "user", "content": "Hello!"}]\n' +
      "  }'";
  }
  function curlVision(id) {
    return "curl " + BASE + "/chat/completions \\\n" +
      '  -H "Authorization: Bearer $LUMEN_API_KEY" \\\n' +
      '  -H "Content-Type: application/json" \\\n' +
      "  -d '{\n" +
      '    "model": "' + id + '",\n' +
      '    "messages": [{"role": "user", "content": [\n' +
      '      {"type": "text", "text": "What is in this image?"},\n' +
      '      {"type": "image_url", "image_url": {"url": "https://example.com/photo.jpg"}}\n' +
      "    ]}]\n  }'";
  }
  function curlAudioChat(id) {
    // Base64 audio is too large for an inline -d argument, so build the request
    // body in a file (embedding the audio) and post it with -d @file.
    return "# 1) Build the request body, embedding the base64-encoded audio\n" +
      "cat > chat.json << EOF\n" +
      "{\n" +
      '  "model": "' + id + '",\n' +
      '  "messages": [{"role": "user", "content": [\n' +
      '    {"type": "text", "text": "<|audio|> can you transcribe the speech into a written format?"},\n' +
      "    {\"type\": \"input_audio\", \"input_audio\": {\"data\": \"$(base64 < audio.mp3 | tr -d '\\n')\", \"format\": \"mp3\"}}\n" +
      "  ]}]\n" +
      "}\n" +
      "EOF\n\n" +
      "# 2) Send it\n" +
      "curl " + BASE + "/chat/completions \\\n" +
      '  -H "Authorization: Bearer $LUMEN_API_KEY" \\\n' +
      '  -H "Content-Type: application/json" \\\n' +
      "  -d @chat.json";
  }
  function curlTranscribe(id) {
    return "curl " + BASE + "/audio/transcriptions \\\n" +
      '  -H "Authorization: Bearer $LUMEN_API_KEY" \\\n' +
      "  -F file=@audio.mp3 \\\n" +
      "  -F model=" + id;
  }

  // ── Python ─────────────────────────────────────────────────────────────────
  const PY_SETUP =
    "import os\n" +
    "from openai import OpenAI\n\n" +
    "client = OpenAI(\n" +
    '    base_url="' + BASE + '",\n' +
    '    api_key=os.environ["LUMEN_API_KEY"],\n' +
    ")\n";
  function pyText(id) {
    return PY_SETUP + "\n" +
      "resp = client.chat.completions.create(\n" +
      '    model="' + id + '",\n' +
      '    messages=[{"role": "user", "content": "Hello!"}],\n' +
      ")\n" +
      "print(resp.choices[0].message.content)";
  }
  function pyVision(id) {
    return PY_SETUP + "\n" +
      "resp = client.chat.completions.create(\n" +
      '    model="' + id + '",\n' +
      '    messages=[{"role": "user", "content": [\n' +
      '        {"type": "text", "text": "What is in this image?"},\n' +
      '        {"type": "image_url", "image_url": {"url": "https://example.com/photo.jpg"}},\n' +
      "    ]}],\n)\n" +
      "print(resp.choices[0].message.content)";
  }
  function pyAudioChat(id) {
    return "import base64\n" + PY_SETUP + "\n" +
      'with open("audio.mp3", "rb") as f:\n' +
      "    audio_b64 = base64.b64encode(f.read()).decode()\n\n" +
      "resp = client.chat.completions.create(\n" +
      '    model="' + id + '",\n' +
      '    messages=[{"role": "user", "content": [\n' +
      '        {"type": "text", "text": "<|audio|> can you transcribe the speech into a written format?"},\n' +
      '        {"type": "input_audio", "input_audio": {"data": audio_b64, "format": "mp3"}},\n' +
      "    ]}],\n)\n" +
      "print(resp.choices[0].message.content)";
  }
  function pyTranscribe(id) {
    return PY_SETUP + "\n" +
      'with open("audio.mp3", "rb") as f:\n' +
      "    result = client.audio.transcriptions.create(\n" +
      '        model="' + id + '", file=f,\n' +
      "    )\n" +
      "print(result.text)";
  }

  function render() {
    renderOpenCode();

    const sel = select ? select.value : "";
    const model = sel ? modelById(sel) : null;
    const id = model ? model.id : PLACEHOLDER;
    const input = (model && model.input_modalities) || [];
    const generic = !model;
    const hasText = input.indexOf("text") !== -1;
    const hasImage = input.indexOf("image") !== -1;
    const hasAudio = input.indexOf("audio") !== -1;
    // Audio-only models are speech models (e.g. granite-speech); they have no
    // text input so the plain text-chat example is not meaningful for them, but
    // they work through both the transcription endpoint and chat/completions.
    const audioOnly = hasAudio && !hasText;
    const AUDIO_NOTE = "Some audio models require an audio placeholder token in the text — granite-speech uses " +
      "<|audio|>. Check the model's card for the exact token.";

    const curl = document.getElementById("examples-curl");
    const py = document.getElementById("examples-python");
    curl.innerHTML = "";
    py.innerHTML = "";

    if (generic || hasText) {
      curl.appendChild(block("Chat completion", curlText(id)));
      py.appendChild(block("Chat completion", pyText(id)));
    }
    if (hasImage) {
      curl.appendChild(block("Vision (image input)", curlVision(id)));
      py.appendChild(block("Vision (image input)", pyVision(id)));
    }
    if (hasAudio) {
      curl.appendChild(block("Audio in chat", curlAudioChat(id), null, AUDIO_NOTE));
      py.appendChild(block("Audio in chat", pyAudioChat(id), null, AUDIO_NOTE));
    }
    // The transcription endpoint is for speech-to-text (audio-only) models.
    if (audioOnly) {
      curl.appendChild(block("Audio transcription", curlTranscribe(id)));
      py.appendChild(block("Audio transcription", pyTranscribe(id)));
    }
  }

  if (select) select.addEventListener("change", render);
  render();
})();
