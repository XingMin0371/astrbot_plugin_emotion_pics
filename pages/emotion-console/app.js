let bridge = window.AstrBotPluginPage;
const $ = (id) => document.getElementById(id);

const state = { images: [], emotions: {}, uploadEmotion: "" };

function setStatus(msg) {
  $("status").textContent = msg || "";
}

function waitBridge(timeoutMs = 3000) {
  return new Promise((resolve) => {
    const started = Date.now();
    const timer = setInterval(() => {
      if (window.AstrBotPluginPage) {
        clearInterval(timer);
        bridge = window.AstrBotPluginPage;
        resolve(bridge);
      } else if (Date.now() - started > timeoutMs) {
        clearInterval(timer);
        resolve(null);
      }
    }, 80);
  });
}

function unwrap(res) {
  if (Array.isArray(res)) return res;
  if (res && typeof res === "object" && "data" in res) return res.data;
  return res;
}

function okOf(res) {
  return !(res && typeof res === "object" && res.status === "error");
}

async function apiGet(path) {
  if (bridge && bridge.apiGet) return unwrap(await bridge.apiGet(path, {}));
  throw new Error("面板桥接不可用");
}

async function apiPost(path, payload) {
  if (bridge && bridge.apiPost) return unwrap(await bridge.apiPost(path, payload || {}));
  throw new Error("面板桥接不可用");
}

function emotionOptions(selected) {
  return Object.keys(state.emotions)
    .map((name) => `<option value="${name}"${name === selected ? " selected" : ""}>${name}</option>`)
    .join("");
}

function renderEmotionSelect() {
  const sel = $("uploadEmotion");
  const previous = sel.value;
  sel.innerHTML = '<option value="">（未分类）</option>' + emotionOptions(previous);
}

function renderState(data) {
  state.images = data.images || [];
  state.emotions = data.emotions || {};
  renderEmotionSelect();
  $("imageCount").textContent = state.images.length;
  renderGrid();
  renderEmotions();
}

function renderGrid() {
  const grid = $("grid");
  grid.innerHTML = "";
  if (!state.images.length) {
    grid.innerHTML = '<div class="card">还没有图片，先在上面上传一张。</div>';
    return;
  }
  for (const item of state.images) {
    const card = document.createElement("div");
    card.className = "pic";
    const img = document.createElement("img");
    img.alt = item.file || "";
    img.src = item.thumb || "";
    const body = document.createElement("div");
    body.className = "body";
    const name = document.createElement("div");
    name.className = "name";
    name.textContent = item.added_at || item.file || "";
    const sel = document.createElement("select");
    sel.innerHTML = '<option value="">（未分类）</option>' + emotionOptions(item.emotion);
    sel.onchange = async () => {
      const res = await apiPost("assign", { id: item.id, emotion: sel.value });
      if (okOf(res)) {
        setStatus(`已设为「${sel.value || "未分类"}」`);
        load();
      } else {
        setStatus((res && res.message) || "设置失败");
      }
    };
    const del = document.createElement("button");
    del.className = "del";
    del.textContent = "删除";
    del.onclick = async () => {
      if (!del.dataset.armed) {
        del.dataset.armed = "1";
        del.textContent = "确认删除？";
        setTimeout(() => {
          if (del.dataset.armed) {
            delete del.dataset.armed;
            del.textContent = "删除";
          }
        }, 4000);
        return;
      }
      delete del.dataset.armed;
      del.textContent = "删除";
      const res = await apiPost("delete", { id: item.id });
      if (okOf(res)) {
        setStatus("已删除");
        load();
      } else {
        setStatus((res && res.message) || "删除失败");
      }
    };
    body.append(name, sel, del);
    card.append(img, body);
    grid.appendChild(card);
  }
}

function renderEmotions() {
  const box = $("emotionList");
  box.innerHTML = "";
  for (const [name, words] of Object.entries(state.emotions)) {
    const row = document.createElement("div");
    row.className = "emo";
    const tag = document.createElement("span");
    tag.className = "tag";
    tag.textContent = name;
    const input = document.createElement("input");
    input.type = "text";
    input.value = (words || []).join("，");
    input.placeholder = "关键词，用逗号分隔";
    const save = document.createElement("button");
    save.textContent = "保存";
    save.onclick = async () => {
      const res = await apiPost("emotion-save", { name, keywords: input.value });
      if (okOf(res)) {
        setStatus(`已保存「${name}」`);
        load();
      } else {
        setStatus((res && res.message) || "保存失败");
      }
    };
    const del = document.createElement("button");
    del.textContent = "删除";
    del.onclick = async () => {
      if (!del.dataset.armed) {
        del.dataset.armed = "1";
        del.textContent = "确认删除？";
        setTimeout(() => {
          if (del.dataset.armed) {
            delete del.dataset.armed;
            del.textContent = "删除";
          }
        }, 4000);
        return;
      }
      delete del.dataset.armed;
      const res = await apiPost("emotion-delete", { name });
      if (okOf(res)) {
        setStatus(`已删除情绪「${name}」`);
        load();
      } else {
        setStatus((res && res.message) || "删除失败");
      }
    };
    row.append(tag, input, save, del);
    box.appendChild(row);
  }
}

async function load() {
  const data = await apiGet("state");
  if (!data) {
    setStatus("没有读到图库数据");
    return;
  }
  renderState(data);
}

async function boot() {
  const b = await waitBridge(3000);
  bridge = b;
  if (b) {
    try {
      await b.ready();
    } catch (err) {
      setStatus("面板桥接未就绪");
    }
  }
  await load();

  $("uploadBtn").onclick = async () => {
    const file = $("fileInput").files[0];
    if (!file) {
      setStatus("先选一张图片");
      return;
    }
    if (!bridge || !bridge.upload) {
      setStatus("面板桥接不可用，无法上传");
      return;
    }
    const emotion = $("uploadEmotion").value;
    $("uploadBtn").disabled = true;
    $("uploadBtn").textContent = "上传中…";
    try {
      const res = unwrap(await bridge.upload("upload", file));
      if (!okOf(res)) throw new Error((res && res.message) || "上传失败");
      const id = (res && res.id) || (res && res.data && res.data.id);
      if (id && emotion) {
        await apiPost("assign", { id, emotion });
      }
      setStatus(emotion ? `已上传并标为「${emotion}」` : "已上传，记得选情绪");
      $("fileInput").value = "";
      await load();
    } catch (err) {
      setStatus(`上传失败：${err && err.message ? err.message : err}`);
    } finally {
      $("uploadBtn").disabled = false;
      $("uploadBtn").textContent = "上传";
    }
  };

  $("addEmotion").onclick = async () => {
    const name = $("newEmotion").value.trim();
    if (!name) {
      setStatus("先填情绪名");
      return;
    }
    const res = await apiPost("emotion-save", { name, keywords: "" });
    if (okOf(res)) {
      $("newEmotion").value = "";
      setStatus(`已添加情绪「${name}」`);
      await load();
    } else {
      setStatus((res && res.message) || "添加失败");
    }
  };
}

boot().catch((err) => setStatus(`加载失败：${err && err.message ? err.message : err}`));
