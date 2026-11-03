// Leave forms: one day and half day ask for a single date, more than one day asks for From and To.
document.querySelectorAll("[data-leave-length]").forEach(form => {
  const startBox = form.querySelector("[data-leave-start]");
  const startLabel = form.querySelector("[data-leave-start-label]");
  const endBox = form.querySelector("[data-leave-end]");
  const halfBox = form.querySelector("[data-leave-half]");
  const end = endBox.querySelector("input");

  function show() {
    const length = (form.querySelector("[name=length]:checked") || {}).value || "one";
    startLabel.textContent = length === "many" ? "From" : "Date";
    startBox.classList.toggle("full", length === "one");
    endBox.hidden = length !== "many";
    end.required = length === "many";
    halfBox.hidden = length !== "half";
  }

  form.querySelectorAll("[name=length]").forEach(r => r.addEventListener("change", show));
  show();
});
