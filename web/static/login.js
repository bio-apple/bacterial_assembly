(() => {
  const password = document.querySelector("#password");
  const toggle = document.querySelector("#toggle-password");
  if (!password || !toggle) return;

  toggle.addEventListener("click", () => {
    const reveal = password.type === "password";
    password.type = reveal ? "text" : "password";
    toggle.textContent = reveal ? "隐藏" : "显示";
    toggle.setAttribute("aria-label", reveal ? "隐藏密码" : "显示密码");
    toggle.setAttribute("aria-pressed", String(reveal));
  });
})();
