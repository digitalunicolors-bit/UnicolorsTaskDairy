/* @mention autocomplete + comment posting for the Unicolors task app. */
(function () {
  // People list is printed on the page by Django's json_script filter.
  function readPeople(id) {
    const el = document.getElementById(id);
    try { return el ? JSON.parse(el.textContent) : []; } catch (e) { return []; }
  }

  // Turn a textarea into one where typing "@" shows matching people.
  function attach(ta, people) {
    if (!ta || ta.dataset.mentionsReady || !people.length) return;
    ta.dataset.mentionsReady = "1";

    const wrap = document.createElement("div");
    wrap.className = "mention-wrap";
    ta.parentNode.insertBefore(wrap, ta);
    wrap.appendChild(ta);
    const list = document.createElement("div");
    list.className = "mention-list";
    list.hidden = true;
    wrap.appendChild(list);

    let matches = [], active = 0, start = -1;

    function currentQuery() {
      const pos = ta.selectionStart;
      const m = ta.value.slice(0, pos).match(/(^|[\s(])@([\w.@+-]*)$/);
      if (!m) return null;
      start = pos - m[2].length - 1;
      return m[2].toLowerCase();
    }

    function close() { list.hidden = true; matches = []; }

    function render() {
      const q = currentQuery();
      if (q === null) return close();
      matches = people.filter(p => p.u.toLowerCase().includes(q) || p.n.toLowerCase().includes(q)).slice(0, 6);
      if (!matches.length) return close();
      active = Math.min(active, matches.length - 1);
      list.innerHTML = "";
      matches.forEach((p, i) => {
        const item = document.createElement("div");
        item.className = "mention-item" + (i === active ? " on" : "");
        const av = document.createElement("span");
        av.className = "avatar xs";
        av.textContent = p.i;
        const name = document.createElement("b");
        name.textContent = p.n;
        const extra = document.createElement("span");
        extra.className = "muted small";
        extra.textContent = "@" + p.u + (p.d ? " · " + p.d : "");
        item.append(av, name, extra);
        item.addEventListener("mousedown", e => { e.preventDefault(); pick(p); });
        list.append(item);
      });
      list.hidden = false;
    }

    function pick(p) {
      const pos = ta.selectionStart;
      const insert = "@" + p.u + " ";
      ta.value = ta.value.slice(0, start) + insert + ta.value.slice(pos);
      const caret = start + insert.length;
      ta.setSelectionRange(caret, caret);
      ta.focus();
      close();
    }

    ta.addEventListener("input", () => { active = 0; render(); });
    ta.addEventListener("keydown", e => {
      if (list.hidden || !matches.length) return;
      if (e.key === "ArrowDown") { e.preventDefault(); active = (active + 1) % matches.length; render(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); active = (active - 1 + matches.length) % matches.length; render(); }
      else if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); e.stopPropagation(); pick(matches[active]); }
      else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(); }
    });
    ta.addEventListener("blur", () => setTimeout(close, 150));
  }

  // Post / delete comments without reloading the page.
  function initComments(root, people) {
    if (!root) return;
    root.querySelectorAll("textarea[data-mentions]").forEach(t => attach(t, people));
    root.querySelectorAll("form[data-comment-form]").forEach(form => {
      if (form.dataset.ready) return;
      form.dataset.ready = "1";

      const ta = form.querySelector("textarea");
      if (ta) ta.addEventListener("keydown", e => {
        if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); form.requestSubmit(); }
      });

      form.addEventListener("submit", async e => {
        e.preventDefault();
        if (form.dataset.confirm && !confirm(form.dataset.confirm)) return;
        const btn = form.querySelector("button");
        if (btn) btn.disabled = true;
        try {
          const r = await fetch(form.action, {
            method: "POST",
            body: new FormData(form),
            headers: { "X-Requested-With": "fetch" },
          });
          if (!r.ok) throw new Error(r.status);
          const holder = document.createElement("div");
          holder.innerHTML = await r.text();
          const fresh = holder.querySelector("[data-comments]");
          root.querySelector("[data-comments]").replaceWith(fresh);
          initComments(root, people);
          const last = fresh.querySelectorAll(".comment");
          if (last.length) last[last.length - 1].scrollIntoView({ block: "nearest" });
        } catch (err) {
          alert("Couldn't save that. Please check your connection and try again.");
          if (btn) btn.disabled = false;
        }
      });
    });
  }

  window.Mentions = { attach, readPeople, initComments };
})();