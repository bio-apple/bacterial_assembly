(() => {
  const dialog = document.querySelector("#confirm-dialog");
  if (!dialog) return;

  const title = dialog.querySelector("#confirm-dialog-title");
  const copy = dialog.querySelector("#confirm-dialog-copy");
  const accept = dialog.querySelector("#confirm-dialog-accept");

  window.confirmAction = function confirmAction(actionLabel) {
    const label = actionLabel || "取消任务";
    if (typeof dialog.showModal !== "function") return Promise.resolve(true);
    if (dialog.open) return Promise.resolve(false);
    title.textContent = label;
    accept.textContent = label;
    accept.className = label === "重新分析" ? "button button-primary" : "button button-cancel";
    copy.textContent = `确认后将${label}。`;
    return new Promise((resolve) => {
      const finish = () => {
        dialog.removeEventListener("close", finish);
        resolve(dialog.returnValue === "confirm");
      };
      dialog.addEventListener("close", finish);
      dialog.showModal();
    });
  };
})();
