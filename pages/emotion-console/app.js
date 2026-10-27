let bridge = window.AstrBotPluginPage;
const $ = (id) => document.getElementById(id);

const state = { images: [], emotions: {}, uploadEmotion: "", page: 1, pages: 1, total: 0, q: "", tag: "", selected: new Set(), trash: [] };

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

async function apiGet(path, params = {}) {
  if (bridge && bridge.apiGet) return unwrap(await bridge.apiGet(path, params));
  throw new Error("面板桥接不可用");
}

async function apiPost(path, payload) {
  if (bridge && bridge.apiPost) return unwrap(await bridge.apiPost(path, payload || {}));
  throw new Error("面板桥接不可用");
}

function esc(value) {
  return String(value).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

function emotionOptions(selected) {
  return Object.keys(state.emotions)
    .map((name) => `<option value="${esc(name)}"${name === selected ? " selected" : ""}>${esc(name)}</option>`)
    .join("");
}

function renderEmotionSelect() {
  const sel = $("uploadEmotion");
  const previous = sel.value;
  sel.innerHTML = '<option value="">（未分类）</option>' + emotionOptions(previous);
}

function renderState(data, append = false) {
  state.images = append ? state.images.concat(data.images || []) : (data.images || []);
  state.emotions = data.emotions || {};
  state.page = data.page || 1;
  state.pages = data.pages || 1;
  state.total = data.total || state.images.length;
  state.selected.clear();
  renderEmotionSelect();
  $("imageCount").textContent = state.total;
  $("trashCount").textContent = data.trash_count || 0;
  $("neverUsed").textContent = data.never_used == null ? "-" : data.never_used;
  const tags = data.tags || [];
  $("tagSelect").innerHTML = '<option value="">全部标签</option>' +
    tags.map((t) => `<option value="${esc(t)}"${t === state.tag ? " selected" : ""}>${esc(t)}</option>`).join("");
  $("pageInfo").textContent = `第 ${state.page}/${state.pages} 页 · 共 ${state.total} 张`;
  $("prevPage").disabled = state.page <= 1;
  $("nextPage").disabled = state.page >= state.pages;
  $("loadMore").style.display = state.page < state.pages ? "" : "none";
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
  const thumbImgs = {};
  for (const item of state.images) {
    const card = document.createElement("div");
    card.className = "pic";
    const img = document.createElement("img");
    img.alt = item.file || "";
    img.loading = "lazy";
    thumbImgs[item.id] = img;
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
    const check = document.createElement("input");
    check.type = "checkbox";
    check.dataset.id = item.id;
    check.onchange = () => {
      if (check.checked) state.selected.add(item.id); else state.selected.delete(item.id);
    };
    const tagBtn = document.createElement("button");
    const tagText = (item.tags || []).join(" · ");
    tagBtn.textContent = tagText ? `✎ ${tagText}` : "＋ 标签";
    tagBtn.className = "pic-tag";
    tagBtn.title = tagText ? `标签：${tagText}（点击编辑，仅用于搜索）` : "添加标签（仅用于搜索）";
    tagBtn.onclick = async () => {
      const current = (item.tags || []).join(", ");
      const input = prompt("标签，用逗号分隔", current);
      if (input === null) return;
      const tags = input.split(/[,，]/).map((x) => x.trim()).filter(Boolean);
      const res = await apiPost("tags-save", { id: item.id, tags });
      if (okOf(res)) { setStatus("标签已保存"); load(); }
      else setStatus((res && res.message) || "保存失败");
    };
    check.className = "pick";
    check.title = "选中";
    const mainRow = document.createElement("div");
    mainRow.className = "pic-main";
    mainRow.append(sel, del);
    body.append(name, mainRow);
    const toolbar = document.createElement("div");
    toolbar.className = "pic-toolbar";
    toolbar.append(tagBtn, check);
    card.prepend(toolbar);
    card.append(img, body);
    grid.appendChild(card);
  }
  (async () => {
    try {
      const ids = state.images.map((x) => x.id).join(",");
      const data = await apiGet("thumbs-data", { ids });
      const map = (data && data.thumbs) || {};
      for (const id of Object.keys(map)) {
        if (thumbImgs[id]) thumbImgs[id].src = map[id];
      }
    } catch (_) {
      for (const item of state.images) {
        try {
          const data = await apiGet("thumb-data", { id: item.id });
          if (data && data.thumb && thumbImgs[item.id]) thumbImgs[item.id].src = data.thumb;
        } catch (_) {}
      }
    }
  })();
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

async function load(page = 1, append = false) {
  const query = encodeURIComponent(state.q || "");
  const tag = encodeURIComponent(state.tag || "");
  const data = await apiGet("state", { page, page_size: 24, q: state.q || "", tag: state.tag || "" });
  if (!data) {
    setStatus("没有读到图库数据");
    return;
  }
  renderState(data, append);
}

async function loadTrash() {
  const data = await apiGet("trash");
  state.trash = (data && data.images) || [];
  const box = $("trashBox");
  box.innerHTML = "";
  if (!state.trash.length) {
    box.innerHTML = '<div class="hint">回收站是空的。</div>';
    return;
  }
  for (const item of state.trash) {
    const row = document.createElement("div");
    row.className = "emo";
    const label = document.createElement("span");
    label.textContent = `${item.file || item.id} · 删除于 ${item.deleted_at || "-"}`;
    const restore = document.createElement("button");
    restore.textContent = "恢复";
    restore.onclick = async () => {
      const res = await apiPost("restore", { id: item.id });
      if (okOf(res)) { setStatus("已恢复"); await load(); await loadTrash(); }
      else setStatus((res && res.message) || "恢复失败");
    };
    row.append(label, restore);
    box.appendChild(row);
  }
  const clear = document.createElement("button");
  clear.textContent = "清空回收站";
  clear.onclick = async () => {
    if (!confirm("彻底删除回收站里的所有图片？")) return;
    const res = await apiPost("trash-clear", {});
    if (okOf(res)) { setStatus("回收站已清空"); await load(); await loadTrash(); }
  };
  box.appendChild(clear);
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

  $("searchBtn").onclick = async () => {
    state.q = $("searchInput").value.trim();
    state.tag = $("tagSelect").value;
    await load(1);
  };
  $("prevPage").onclick = () => load(Math.max(1, state.page - 1));
  $("nextPage").onclick = () => load(Math.min(state.pages, state.page + 1));
  $("loadMore").onclick = () => load(state.page + 1, true);
  if ("IntersectionObserver" in window) {
    const observer = new IntersectionObserver((entries) => {
      if (entries[0].isIntersecting && state.page < state.pages) load(state.page + 1, true);
    }, { rootMargin: "240px" });
    observer.observe($("loadMore"));
  }
  $("trashBtn").onclick = loadTrash;
  $("restoreBackupBtn").onclick = async () => {
    if (!confirm("用最新备份覆盖当前图库索引和图片？")) return;
    const data = await apiPost("restore-backup", {});
    if (okOf(data)) { setStatus("已从备份恢复"); await load(1); }
    else setStatus((data && data.message) || "恢复失败");
  };
  $("backupBtn").onclick = async () => {
    const data = await apiPost("backup", {});
    if (okOf(data)) setStatus(`备份完成：${data.path}`);
    else setStatus((data && data.message) || "备份失败");
  };
  $("exportBtn").onclick = async () => {
    const data = await apiGet("export");
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "emotion_pics_index.json";
    a.click();
    URL.revokeObjectURL(a.href);
  };
  $("importInput").onchange = async (ev) => {
    const file = ev.target.files[0];
    if (!file) return;
    try {
      const payload = JSON.parse(await file.text());
      const res = await apiPost("import", payload);
      if (okOf(res)) { setStatus("导入完成"); await load(1); }
      else setStatus((res && res.message) || "导入失败");
    } catch (err) {
      setStatus("导入文件不是有效 JSON");
    }
    ev.target.value = "";
  };
  $("selectAllBtn").onclick = () => {
    const boxes = document.querySelectorAll("#grid input[type=checkbox]");
    const allOn = [...boxes].every((b) => b.checked);
    boxes.forEach((b) => { b.checked = !allOn; b.dispatchEvent(new Event("change")); });
  };
  $("bulkDeleteBtn").onclick = async () => {
    if (!state.selected.size) { setStatus("先勾选图片"); return; }
    if (!confirm(`删除选中的 ${state.selected.size} 张图片？`)) return;
    const res = await apiPost("bulk-delete", { ids: [...state.selected] });
    if (okOf(res)) { setStatus("已移到回收站"); await load(1); }
    else setStatus((res && res.message) || "删除失败");
  };

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
      if (res && res.duplicate) {
        const merge = emotion && confirm(`图库里已有同一张，把情绪「${emotion}」合并到已有图片？`);
        if (merge) await apiPost("assign", { id, emotion });
        setStatus(merge ? "已合并情绪到已有图片" : "图库里已有同一张，已跳过");
        await load();
        return;
      }
      if (id && emotion) {
        await apiPost("assign", { id, emotion });
      }
      const tags = $("uploadTags").value.split(/[,，]/).map((x) => x.trim()).filter(Boolean);
      if (id && tags.length) {
        const tagRes = await apiPost("tags-save", { id, tags });
        if (!okOf(tagRes)) setStatus("图片已上传，但标签保存失败，请点卡片上的标签重试");
      }
      setStatus(emotion ? `已上传并标为「${emotion}」` : "已上传，记得选情绪");
      $("fileInput").value = "";
      $("uploadTags").value = "";
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
