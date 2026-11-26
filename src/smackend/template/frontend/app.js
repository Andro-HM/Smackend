const statusText = document.querySelector("#status");
const checkButton = document.querySelector("#check");

async function checkStatus() {
  checkButton.disabled = true;
  statusText.textContent = "Checking the application and database…";
  try {
    const health = await fetch("/api/health", { cache: "no-store" });
    if (!health.ok) throw new Error("Health check failed");
    const readiness = await fetch("/api/ready", { cache: "no-store" });
    statusText.textContent = readiness.ok
      ? "Django Ninja and PostgreSQL are ready."
      : "The application is running. PostgreSQL is unavailable.";
  } catch {
    statusText.textContent = "The application could not be reached.";
  } finally {
    checkButton.disabled = false;
  }
}

checkButton.addEventListener("click", checkStatus);
checkStatus();
