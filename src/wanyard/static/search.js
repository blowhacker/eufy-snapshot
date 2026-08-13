const form = document.getElementById("searchForm");
const query = document.getElementById("searchQuery");
const submit = document.getElementById("searchSubmit");
const result = document.getElementById("searchResult");
const empty = document.getElementById("searchEmpty");
const answer = document.getElementById("searchAnswer");
const confidence = document.getElementById("searchConfidence");
const scope = document.getElementById("searchScope");
const limitation = document.getElementById("searchLimitation");
const evidence = document.getElementById("searchEvidence");

function eventTime(ts) {
  return new Intl.DateTimeFormat(undefined, {
    weekday: "short", hour: "2-digit", minute: "2-digit", second: "2-digit",
  }).format(new Date(Number(ts) * 1000));
}

function renderEvidence(events) {
  evidence.replaceChildren();
  for (const event of events) {
    const link = document.createElement("a");
    link.className = "evidence-card";
    link.href = event.target_url;

    const image = document.createElement("img");
    image.src = event.thumb_url;
    image.alt = `${event.class} evidence from ${event.source_name}`;
    image.loading = "lazy";

    const meta = document.createElement("span");
    meta.className = "evidence-meta";
    const title = document.createElement("strong");
    title.textContent = event.class;
    const detail = document.createElement("span");
    detail.textContent = `${event.source_name} · ${eventTime(event.abs_ts)}`;
    meta.append(title, detail);
    link.append(image, meta);
    evidence.append(link);
  }
}

async function runSearch(text) {
  submit.disabled = true;
  submit.classList.add("loading");
  submit.querySelector("span").textContent = "Searching";
  try {
    const response = await fetch("/api/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query: text }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `Search failed (${response.status})`);
    answer.textContent = data.answer;
    confidence.textContent = data.confidence;
    scope.textContent = data.plan?.time_label || "";
    const note = (data.limitations || []).join(" ");
    limitation.textContent = note;
    limitation.hidden = !note;
    renderEvidence(data.evidence || []);
  } catch (error) {
    answer.textContent = error.message || "Search failed.";
    confidence.textContent = "error";
    scope.textContent = "";
    limitation.hidden = true;
    evidence.replaceChildren();
  } finally {
    result.hidden = false;
    empty.hidden = true;
    submit.disabled = false;
    submit.classList.remove("loading");
    submit.querySelector("span").textContent = "Search recordings";
  }
}

form.addEventListener("submit", event => {
  event.preventDefault();
  const text = query.value.trim();
  if (text) runSearch(text);
});

document.querySelectorAll(".search-prompts button").forEach(button => {
  button.addEventListener("click", () => {
    query.value = button.textContent;
    runSearch(query.value);
  });
});
