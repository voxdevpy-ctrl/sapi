"use strict";
/* Entrada do SAPI: uma porta industrial que você levanta (deslizando para cima no celular, rolando para baixo
   no computador, arrastando com o mouse ou pelo botão Entrar). Aparece uma vez por sessão do navegador.
   NÃO é segurança: o PIN do acesso remoto (#login) continua separado e aparece por cima quando o servidor exige.
   Se qualquer coisa falhar aqui, a porta é removida e a Central fica acessível. */
(() => {
  const gate = document.getElementById("sapi-gate");
  const html = document.documentElement;
  if (!gate) return;
  const bail = () => { try { gate.remove(); } catch { /* já saiu */ } html.classList.remove("gate"); };
  try {
    if (!html.classList.contains("gate")) { gate.remove(); return; }
    const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
    const shutter = gate.querySelector(".gate-shutter"), gear = gate.querySelector(".gate-gear"),
      fill = gate.querySelector(".gate-fill"), enter = gate.querySelector(".gate-enter");
    enter.removeAttribute("onclick"); // o fallback sem JS deixa de ser necessário
    let progress = 0, startY = 0, dragging = false, opened = false, wheelAcc = 0, wheelTimer = 0;
    const limiar = () => Math.min(innerHeight * 0.3, 260); // quanto precisa levantar para abrir

    function set(p) {
      progress = Math.max(0, Math.min(1.15, p));
      shutter.style.transform = `translate3d(0, ${(-progress * limiar()).toFixed(1)}px, 0)`;
      gear.style.transform = `rotate(${(progress * 150).toFixed(1)}deg)`;
      fill.style.transform = `scaleX(${Math.min(1, progress).toFixed(3)})`;
      gate.classList.toggle("armed", progress >= 1);
    }
    function reset() { shutter.style.transition = ""; gear.style.transition = ""; set(0); }
    function open() {
      if (opened) return;
      opened = true;
      try { sessionStorage.setItem("sapi_gate", "1"); } catch { /* sem armazenamento: só abre */ }
      clearTimeout(wheelTimer);
      shutter.style.transition = ""; shutter.style.transform = "";
      gate.classList.add("open");
      html.classList.remove("gate");
      const done = () => { gate.remove(); gate.dispatchEvent(new Event("sapi:aberto")); };
      if (reduce) { done(); return; }
      shutter.addEventListener("transitionend", done, { once: true });
      setTimeout(done, 1100); // garantia: nunca fica preso esperando a transição
    }

    // arrastar (toque ou mouse)
    gate.addEventListener("pointerdown", (e) => {
      if (e.button > 0 || e.target.closest("button, a")) return;
      dragging = true; startY = e.clientY; wheelAcc = 0;
      shutter.style.transition = "none"; gear.style.transition = "none";
      gate.setPointerCapture(e.pointerId);
    });
    gate.addEventListener("pointermove", (e) => { if (dragging) set((startY - e.clientY) / limiar()); });
    const solta = () => { if (!dragging) return; dragging = false; progress >= 1 ? open() : reset(); };
    gate.addEventListener("pointerup", solta);
    gate.addEventListener("pointercancel", solta);

    // roda do mouse / touchpad: rolar para baixo levanta a porta
    gate.addEventListener("wheel", (e) => {
      e.preventDefault();
      if (opened || e.deltaY <= 0) return;
      wheelAcc += e.deltaY;
      shutter.style.transition = "transform .12s linear"; gear.style.transition = "transform .12s linear";
      set(wheelAcc / (limiar() * 1.6));
      clearTimeout(wheelTimer);
      if (progress >= 1) open();
      else wheelTimer = setTimeout(() => { wheelAcc = 0; reset(); }, 450);
    }, { passive: false });

    // teclado e botão
    gate.addEventListener("keydown", (e) => { if (["Enter", " ", "ArrowUp", "PageDown"].includes(e.key)) { e.preventDefault(); open(); } });
    enter.addEventListener("click", open);
    gate.tabIndex = -1;
    gate.focus({ preventScroll: true });
    // se o PIN do acesso remoto aparecer, a porta sai da frente: os dois nunca coexistem
    const login = document.getElementById("login");
    if (login) new MutationObserver(() => { if (!login.hidden) bail(); }).observe(login, { attributes: true, attributeFilter: ["hidden"] });
  } catch (e) {
    bail();
  }
})();
