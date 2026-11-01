// Schulkasse - small progressive enhancements. Everything works without JS
// except the live remainder display in the split editor.
"use strict";

function parseCent(text) {
  let s = String(text || "").replace(/[€\s ]/g, "");
  if (!s) return 0;
  if (s.includes(",") && s.includes(".")) s = s.replace(/\./g, "").replace(",", ".");
  else if (s.includes(",")) s = s.replace(",", ".");
  else if (s.includes(".")) {
    const parts = s.split(".");
    if (parts.length > 2 || parts[parts.length - 1].length === 3) s = s.replace(/\./g, "");
  }
  if (!/^-?\d+(\.\d{1,2})?$/.test(s)) return NaN;
  const [e, c = ""] = s.replace("-", "").split(".");
  const cent = parseInt(e, 10) * 100 + parseInt((c + "00").slice(0, 2), 10);
  return s.startsWith("-") ? -cent : cent;
}

function formatCent(cent) {
  const neg = cent < 0;
  cent = Math.abs(cent);
  const euro = Math.floor(cent / 100).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ".");
  return (neg ? "-" : "") + euro + "," + String(cent % 100).padStart(2, "0") + " €";
}

document.addEventListener("click", (ev) => {
  const el = ev.target.closest("[data-confirm]");
  if (el && !window.confirm(el.dataset.confirm)) { ev.preventDefault(); return; }
  const knopf = ev.target.closest("[data-text-quelle]");
  if (knopf) {
    const quelle = document.getElementById(knopf.dataset.textQuelle);
    const ziel = document.getElementById(knopf.dataset.textZiel);
    if (quelle && ziel) ziel.value = quelle.value;
  }
});

// Header drop-down menus (<details class="menu">): only one open at a time,
// closed by a click outside or by Escape.
function menuesSchliessen(ausser) {
  document.querySelectorAll("details.menu[open]").forEach((m) => { if (m !== ausser) m.open = false; });
}
document.addEventListener("toggle", (ev) => {
  const m = ev.target;
  if (m.matches && m.matches("details.menu") && m.open) menuesSchliessen(m);
}, true);  // "toggle" does not bubble, so listen in the capture phase
document.addEventListener("click", (ev) => {
  if (!ev.target.closest("details.menu")) menuesSchliessen(null);
});
document.addEventListener("keydown", (ev) => {
  if (ev.key !== "Escape") return;
  const offen = ev.target.closest && ev.target.closest("details.menu[open]");
  menuesSchliessen(null);
  if (offen) offen.querySelector("summary").focus();
});

document.addEventListener("change", (ev) => {
  const t = ev.target;
  if (t.matches("[data-alle]")) {
    document.querySelectorAll(t.dataset.alle).forEach((cb) => { if (!cb.disabled) cb.checked = t.checked; });
  }
  if (t.matches("select[data-autosubmit]")) t.form.submit();
});

// Project form: only the classes of the chosen school year; grade buttons
// select (or deselect) all classes of a grade.
function initKlassenwahl(fs) {
  const sj = document.getElementById("schuljahr_id");
  function filtern() {
    fs.querySelectorAll(".klassengruppe").forEach((g) => {
      const passt = !sj || g.dataset.schuljahr === sj.value;
      g.hidden = !passt;
      g.querySelectorAll("input").forEach((cb) => { cb.disabled = !passt; });
    });
  }
  if (sj) sj.addEventListener("change", filtern);
  filtern();
  fs.addEventListener("click", (ev) => {
    const knopf = ev.target.closest("button[data-jahrgang]");
    if (!knopf) return;
    const boxen = knopf.closest(".klassengruppe").querySelectorAll(`input[data-jahrgang="${knopf.dataset.jahrgang}"]`);
    const alle = Array.from(boxen).every((cb) => cb.checked);
    boxen.forEach((cb) => { cb.checked = !alle; });
  });
}

// Split editor (F5.4)
function initAufteilung(form) {
  const gesamt = parseInt(form.dataset.rest, 10);
  const richtung = form.dataset.richtung;
  const liste = form.querySelector(".teile");
  const vorlage = form.querySelector("template");
  const anzeige = form.querySelector(".restanzeige");
  const speichern = form.querySelector("button[type=submit]");

  function aktualisieren() {
    let summe = 0, ungueltig = false;
    liste.querySelectorAll("input[name=betrag]").forEach((inp) => {
      const c = parseCent(inp.value);
      if (Number.isNaN(c)) ungueltig = true; else summe += Math.abs(c);
    });
    const rest = gesamt - summe;
    anzeige.textContent = ungueltig ? "Ungültiger Betrag" : "Noch zu verteilen: " + formatCent(rest);
    anzeige.classList.toggle("ueber", rest < 0 || ungueltig);
    anzeige.classList.toggle("null", rest === 0 && !ungueltig);
    speichern.disabled = rest < 0 || ungueltig || summe === 0;
  }

  async function ladePosten(zeile) {
    const projekt = zeile.querySelector("select[name=projekt_id]").value;
    const sel = zeile.querySelector("select[name=posten_id]");
    sel.innerHTML = '<option value="">– ohne Posten (nur Projekt) –</option>';
    if (!projekt) return;
    const resp = await fetch(form.dataset.postenUrl.replace("0", projekt) + "?richtung=" + richtung,
      { credentials: "same-origin" });
    if (!resp.ok) return;
    (await resp.json()).forEach((p) => {
      const o = document.createElement("option");
      o.value = p.id; o.textContent = p.text; o.dataset.rest = p.rest_cent;
      sel.appendChild(o);
    });
    if (zeile.dataset.posten) { sel.value = zeile.dataset.posten; delete zeile.dataset.posten; }
  }

  function neueZeile(prefill) {
    const node = vorlage.content.firstElementChild.cloneNode(true);
    liste.appendChild(node);
    if (prefill) node.querySelector("input[name=betrag]").value = prefill;
    aktualisieren();
    return node;
  }

  liste.addEventListener("change", (ev) => {
    const zeile = ev.target.closest(".teilzeile");
    if (ev.target.name === "projekt_id") ladePosten(zeile);
    if (ev.target.name === "posten_id") {
      const opt = ev.target.selectedOptions[0];
      const inp = zeile.querySelector("input[name=betrag]");
      if (opt && opt.dataset.rest && !inp.value) {
        let offen = gesamt;
        liste.querySelectorAll("input[name=betrag]").forEach((i) => { if (i !== inp) offen -= Math.abs(parseCent(i.value) || 0); });
        inp.value = formatCent(Math.min(parseInt(opt.dataset.rest, 10), offen)).replace(" €", "");
      }
    }
    aktualisieren();
  });
  liste.addEventListener("input", aktualisieren);
  liste.addEventListener("click", (ev) => {
    if (ev.target.matches(".entfernen")) { ev.target.closest(".teilzeile").remove(); aktualisieren(); }
  });
  form.querySelector(".hinzu").addEventListener("click", () => {
    let offen = gesamt;
    liste.querySelectorAll("input[name=betrag]").forEach((i) => { offen -= Math.abs(parseCent(i.value) || 0); });
    neueZeile(offen > 0 ? formatCent(offen).replace(" €", "") : "");
  });

  const codeFeld = form.querySelector("[data-zahlcode]");
  if (codeFeld) {
    codeFeld.addEventListener("keydown", async (ev) => {
      if (ev.key !== "Enter") return;
      ev.preventDefault();
      const resp = await fetch(form.dataset.codeUrl + "?code=" + encodeURIComponent(codeFeld.value), { credentials: "same-origin" });
      const hinweis = form.querySelector(".code-hinweis");
      if (!resp.ok) { hinweis.textContent = "Zahlcode nicht gefunden."; return; }
      const d = await resp.json();
      let zeile = [...liste.querySelectorAll(".teilzeile")].find((z) => !z.querySelector("select[name=projekt_id]").value);
      if (!zeile) zeile = neueZeile("");
      zeile.querySelector("select[name=projekt_id]").value = d.projekt_id;
      zeile.dataset.posten = d.posten_id;
      await ladePosten(zeile);
      zeile.querySelector("select[name=posten_id]").dispatchEvent(new Event("change", { bubbles: true }));
      hinweis.textContent = "Posten " + d.zahlcode + " übernommen.";
      codeFeld.value = "";
    });
  }
  if (!liste.children.length) neueZeile(formatCent(gesamt).replace(" €", ""));
  aktualisieren();
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("form.aufteilung").forEach(initAufteilung);
  document.querySelectorAll("fieldset.klassenwahl").forEach(initKlassenwahl);
});
