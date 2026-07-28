// Job Application Autofill - content script
// Only runs when triggered by clicking "Fill This Page" in the popup.
// Never runs automatically, never submits anything.

function setNativeValue(element, value) {
  // Standard technique for making React/Vue-controlled inputs actually
  // register the change (plain element.value = ... gets silently
  // overwritten by frameworks that track their own state).
  const prototype = Object.getPrototypeOf(element);
  const prototypeValueSetter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
  const ownValueSetter = Object.getOwnPropertyDescriptor(element, "value")?.set;

  if (prototypeValueSetter && ownValueSetter !== prototypeValueSetter) {
    prototypeValueSetter.call(element, value);
  } else {
    element.value = value;
  }

  element.dispatchEvent(new Event("input", { bubbles: true }));
  element.dispatchEvent(new Event("change", { bubbles: true }));
}

function getFieldLabelText(el) {
  // Try <label for="id">, wrapping <label>, aria-label, placeholder, name/id
  let text = "";
  if (el.id) {
    const label = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    if (label) text += " " + label.textContent;
  }
  const parentLabel = el.closest("label");
  if (parentLabel) text += " " + parentLabel.textContent;
  if (el.getAttribute("aria-label")) text += " " + el.getAttribute("aria-label");
  if (el.placeholder) text += " " + el.placeholder;
  text += " " + (el.name || "") + " " + (el.id || "") + " " + (el.autocomplete || "");
  return text.toLowerCase();
}

function matchField(labelText, autocompleteAttr) {
  const t = labelText;
  const ac = (autocompleteAttr || "").toLowerCase();

  if (ac === "given-name" || /\bfirst.?name\b|\bfname\b/.test(t)) return "firstName";
  if (ac === "family-name" || /\blast.?name\b|\blname\b|\bsurname\b/.test(t)) return "lastName";
  if (ac === "email" || /\bemail\b/.test(t)) return "email";
  if (ac === "tel" || /\bphone\b|\bmobile\b|\btel\b/.test(t)) return "phone";
  if (ac === "address-line1" || /\bstreet\b|\baddress\b(?!.*(line2|apt|suite))/.test(t)) return "address";
  if (ac === "address-level2" || /\bcity\b|\btown\b/.test(t)) return "city";
  if (ac === "address-level1" || /\bstate\b|\bprovince\b/.test(t)) return "state";
  if (ac === "postal-code" || /\bzip\b|\bpostal\b/.test(t)) return "zip";
  if (/\blinkedin\b/.test(t)) return "linkedin";
  if (/\bportfolio\b|\bgithub\b|\bpersonal.?website\b/.test(t)) return "portfolio";
  if (/\bwork.?authoriz|\beligib(le|ility).*(work|employ)|\blegally.*(work|employed)\b/.test(t)) return "workAuthorized";
  if (/\bsponsor(ship)?\b|\bvisa\b/.test(t)) return "needsSponsorship";
  if (/\bschool\b|\buniversity\b|\bcollege\b/.test(t)) return "school";
  if (/\bdegree\b|\bmajor\b|\bprogram\b/.test(t)) return "degree";

  return null;
}

const STATE_NAMES = {
  al:"alabama",ak:"alaska",az:"arizona",ar:"arkansas",ca:"california",co:"colorado",
  ct:"connecticut",de:"delaware",fl:"florida",ga:"georgia",hi:"hawaii",id:"idaho",
  il:"illinois",in:"indiana",ia:"iowa",ks:"kansas",ky:"kentucky",la:"louisiana",
  me:"maine",md:"maryland",ma:"massachusetts",mi:"michigan",mn:"minnesota",
  ms:"mississippi",mo:"missouri",mt:"montana",ne:"nebraska",nv:"nevada",
  nh:"new hampshire",nj:"new jersey",nm:"new mexico",ny:"new york",
  nc:"north carolina",nd:"north dakota",oh:"ohio",ok:"oklahoma",or:"oregon",
  pa:"pennsylvania",ri:"rhode island",sc:"south carolina",sd:"south dakota",
  tn:"tennessee",tx:"texas",ut:"utah",vt:"vermont",va:"virginia",wa:"washington",
  wv:"west virginia",wi:"wisconsin",wy:"wyoming"
};

function selectOptionFuzzy(el, value) {
  const target = value.trim().toLowerCase();
  const options = Array.from(el.options);

  // 1. Exact match first
  for (const opt of options) {
    if (opt.text.trim().toLowerCase() === target) {
      setNativeValue(el, opt.value);
      return true;
    }
  }

  // 2. State abbreviation <-> full name (e.g. "NC" matches "North Carolina")
  const abbrev = target.length === 2 ? target : null;
  const fullName = STATE_NAMES[target] || null;
  for (const opt of options) {
    const optText = opt.text.trim().toLowerCase();
    if (abbrev && optText === STATE_NAMES[abbrev]) {
      setNativeValue(el, opt.value);
      return true;
    }
    if (fullName && optText === fullName) {
      setNativeValue(el, opt.value);
      return true;
    }
    // reverse: profile has full name, dropdown has abbreviation
    for (const [ab, full] of Object.entries(STATE_NAMES)) {
      if (target === full && optText === ab) {
        setNativeValue(el, opt.value);
        return true;
      }
    }
  }

  // 3. Partial/substring match either direction (handles dropdowns that
  // don't have your exact degree wording, e.g. "Associate Degree" vs
  // "Associate in Engineering")
  let bestMatch = null;
  let bestScore = 0;
  for (const opt of options) {
    const optText = opt.text.trim().toLowerCase();
    if (!optText) continue;
    if (optText.includes(target) || target.includes(optText)) {
      const score = Math.min(optText.length, target.length);
      if (score > bestScore) {
        bestScore = score;
        bestMatch = opt;
      }
    }
  }
  if (bestMatch) {
    setNativeValue(el, bestMatch.value);
    return true;
  }

  return false;
}

function fillYesNoField(el, value) {
  const tag = el.tagName.toLowerCase();
  if (tag === "select") {
    return selectOptionFuzzy(el, value);
  } else if (el.type === "radio") {
    const labelText = getFieldLabelText(el);
    if (labelText.includes(value.toLowerCase())) {
      el.click();
      return true;
    }
  }
  return false;
}

function highlightFilled(el) {
  el.style.outline = "2px solid #1a7f37";
  el.style.outlineOffset = "1px";
}

function fillForm(profile) {
  let filledCount = 0;
  const skippedFileInputs = [];

  const inputs = document.querySelectorAll("input, select, textarea");

  for (const el of inputs) {
    if (el.type === "file") {
      skippedFileInputs.push(el);
      continue; // browsers block scripts from setting file input values - security limit
    }
    if (el.type === "hidden" || el.disabled || el.readOnly) continue;

    // Skip long free-text areas (likely open-ended essay questions) -
    // these need your own words, not auto-generated answers.
    if (el.tagName.toLowerCase() === "textarea" && (el.rows > 3 || (el.maxLength && el.maxLength > 300))) {
      continue;
    }

    const labelText = getFieldLabelText(el);
    const fieldKey = matchField(labelText, el.autocomplete);
    if (!fieldKey || !profile[fieldKey]) continue;

    if (fieldKey === "workAuthorized" || fieldKey === "needsSponsorship") {
      if (el.tagName.toLowerCase() === "select" || el.type === "radio") {
        if (fillYesNoField(el, profile[fieldKey])) {
          highlightFilled(el);
          filledCount++;
        }
        continue;
      }
    }

    if (el.tagName.toLowerCase() === "select") {
      if (selectOptionFuzzy(el, profile[fieldKey])) {
        highlightFilled(el);
        filledCount++;
      }
      continue;
    }

    setNativeValue(el, profile[fieldKey]);
    highlightFilled(el);
    filledCount++;
  }

  if (skippedFileInputs.length > 0) {
    console.log(
      `Job Autofill: ${skippedFileInputs.length} file upload field(s) found - ` +
      `browsers don't allow scripts to attach files automatically. Attach your resume manually.`
    );
  }

  return filledCount;
}

function guessCompanyAndTitle() {
  // Best-effort guess from the page title / meta tags - always review
  // and edit before logging, this is just a starting point.
  const ogTitle = document.querySelector('meta[property="og:title"]')?.content;
  const ogSite = document.querySelector('meta[property="og:site_name"]')?.content;
  const title = ogTitle || document.title || "";

  // Common pattern: "Job Title at Company - Site Name" or "Job Title - Company"
  let guessedTitle = title;
  let guessedCompany = ogSite || "";

  const atMatch = title.match(/^(.*?)\s+at\s+(.*?)(\s*[-|]\s*.*)?$/i);
  if (atMatch) {
    guessedTitle = atMatch[1].trim();
    guessedCompany = atMatch[2].trim();
  }

  return { guessedTitle, guessedCompany };
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === "FILL_FORM") {
    const filledCount = fillForm(message.profile || {});
    sendResponse({ filledCount });
  } else if (message.type === "GET_PAGE_INFO") {
    sendResponse(guessCompanyAndTitle());
  }
  return true;
});
