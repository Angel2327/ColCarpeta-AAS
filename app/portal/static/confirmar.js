/* Confirmacion de acciones destructivas con <dialog> nativo, en vez del window.confirm()
   del navegador (que muestra el dominio -- "127.0.0.1:8000 dice" -- y no se puede
   estilar). Sin ninguna libreria: <dialog> ya trae el foco atrapado y el cierre con
   Escape por su cuenta.

   Sin este script (JS deshabilitado o falla la carga), cada formulario sigue
   enviandose de forma normal: la confirmacion nunca es la unica forma de que la
   accion ocurra, es una capa que se agrega encima. */
(function () {
  "use strict";

  var dialogo = document.getElementById("dialogo-confirmar");
  if (!dialogo) return;
  var texto = document.getElementById("dialogo-confirmar-texto");

  function pedirConfirmacion(mensaje, alConfirmar) {
    texto.textContent = mensaje;
    dialogo.returnValue = "";
    dialogo.showModal();
    dialogo.addEventListener("close", function alCerrar() {
      dialogo.removeEventListener("close", alCerrar);
      if (dialogo.returnValue === "confirmar") alConfirmar();
    });
  }

  // Formularios normales (sin HTMX): cerrar sesion, eliminar desde el detalle de un
  // documento. data-confirmar trae el mensaje a mostrar.
  document.querySelectorAll("form[data-confirmar]").forEach(function (form) {
    form.addEventListener("submit", function (evento) {
      if (form.dataset.confirmado === "si") return; // ya se confirmo, dejar pasar
      evento.preventDefault();
      pedirConfirmacion(form.dataset.confirmar, function () {
        form.dataset.confirmado = "si";
        form.requestSubmit();
      });
    });
  });

  // Formularios con HTMX (eliminar desde la lista de la carpeta): htmx ya intercepta
  // el envio por su cuenta y dispara este evento en vez de window.confirm() cuando el
  // formulario trae hx-confirm -- se reutiliza el mismo dialogo, sin tocar el atributo
  // hx-confirm que ya existe en la plantilla.
  document.body.addEventListener("htmx:confirm", function (evento) {
    if (!evento.detail.question) return;
    evento.preventDefault();
    pedirConfirmacion(evento.detail.question, function () {
      evento.detail.issueRequest(true);
    });
  });
})();
