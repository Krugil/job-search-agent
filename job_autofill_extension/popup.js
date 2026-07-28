const FIELDS = [
  "firstName", "lastName", "email", "phone", "address", "city", "state",
  "zip", "linkedin", "portfolio", "workAuthorized", "needsSponsorship",
  "school", "degree"
];

function loadProfile() {
  chrome.storage.local.get("profile", (data) => {
    const profile = data.profile || {};
    for (const field of FIELDS) {
      const el = document.getElementById(field);
      if (el && profile[field] !== undefined) {
        el.value = profile[field];
      }
    }
  });
}

function saveProfile() {
  const profile = {};
  for (const field of FIELDS) {
    const el = document.getElementById(field);
    if (el) profile[field] = el.value;
  }
  const webAppUrl = document.getElementById("webAppUrl").value.trim();
  chrome.storage.local.set({ profile, webAppUrl }, () => {
    document.getElementById("status").textContent = "Profile saved.";
    setTimeout(() => { document.getElementById("status").textContent = ""; }, 2000);
  });
}

function fillPage() {
  saveProfile();
  chrome.storage.local.get("profile", (data) => {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      chrome.tabs.sendMessage(
        tabs[0].id,
        { type: "FILL_FORM", profile: data.profile || {} },
        (response) => {
          const status = document.getElementById("status");
          if (chrome.runtime.lastError) {
            status.textContent = "Couldn't reach this page. Try reloading it.";
          } else if (response) {
            status.textContent = `Filled ${response.filledCount} field(s). Review before submitting!`;
          }
        }
      );
    });
  });
}

function prefillPageInfo() {
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    chrome.tabs.sendMessage(tabs[0].id, { type: "GET_PAGE_INFO" }, (response) => {
      if (chrome.runtime.lastError || !response) return;
      const companyField = document.getElementById("logCompany");
      const titleField = document.getElementById("logJobTitle");
      if (companyField && !companyField.value) companyField.value = response.guessedCompany || "";
      if (titleField && !titleField.value) titleField.value = response.guessedTitle || "";
    });
  });
}

function loadWebAppUrl() {
  chrome.storage.local.get("webAppUrl", (data) => {
    if (data.webAppUrl) document.getElementById("webAppUrl").value = data.webAppUrl;
  });
}

function logToTracker() {
  const webAppUrl = document.getElementById("webAppUrl").value.trim();
  const company = document.getElementById("logCompany").value.trim();
  const jobTitle = document.getElementById("logJobTitle").value.trim();
  const logStatus = document.getElementById("logStatus");

  if (!webAppUrl) {
    logStatus.textContent = "Paste your Apps Script Web App URL first (one-time setup).";
    return;
  }
  chrome.storage.local.set({ webAppUrl });

  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    const link = tabs[0].url;
    logStatus.textContent = "Logging...";
    fetch(webAppUrl, {
      method: "POST",
      body: JSON.stringify({ company, jobTitle, link }),
    })
      .then((r) => r.json())
      .then((r) => {
        logStatus.textContent = r.success ? "Logged to tracker ✓" : `Error: ${r.error}`;
      })
      .catch((err) => {
        logStatus.textContent = `Request failed: ${err}`;
      });
  });
}

function fillAllTabs() {
  saveProfile();
  chrome.storage.local.get("profile", (data) => {
    const profile = data.profile || {};
    chrome.tabs.query({ currentWindow: true }, (tabs) => {
      const targetTabs = tabs.filter((t) => t.url && (t.url.startsWith("http://") || t.url.startsWith("https://")));
      let completed = 0;
      let totalFilled = 0;
      const status = document.getElementById("status");
      status.textContent = `Filling ${targetTabs.length} tab(s)...`;

      if (targetTabs.length === 0) {
        status.textContent = "No open web pages found to fill.";
        return;
      }

      targetTabs.forEach((tab) => {
        chrome.tabs.sendMessage(tab.id, { type: "FILL_FORM", profile }, (response) => {
          completed++;
          if (response) totalFilled += response.filledCount;
          if (completed === targetTabs.length) {
            status.textContent = `Done - filled ${totalFilled} field(s) across ${targetTabs.length} tab(s). Review each before submitting.`;
            if (chrome.notifications) {
              chrome.notifications.create({
                type: "basic",
                iconUrl: "icon.png",
                title: "Job applications ready to review",
                message: `Filled ${targetTabs.length} tab(s) - go review and submit each one.`,
              }, () => {
                // ignore errors if icon.png doesn't exist - notification may
                // still show without a custom icon on some platforms
              });
            }
          }
        });
      });
    });
  });
}

document.addEventListener("DOMContentLoaded", () => {
  loadProfile();
  loadWebAppUrl();
  prefillPageInfo();
});
document.getElementById("saveBtn").addEventListener("click", saveProfile);
document.getElementById("fillBtn").addEventListener("click", fillPage);
document.getElementById("fillAllBtn").addEventListener("click", fillAllTabs);
document.getElementById("logBtn").addEventListener("click", logToTracker);
