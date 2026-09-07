// Mirrors the abbreviations in backend/app/campus/departments.json. Keep this
// small lookup in the browser so plans saved before display_code was added can
// be upgraded without another SAIS request or deleting a student's schedule.
const DEPARTMENT_ABBREVIATIONS: Record<string, string> = {
  "120": "ARCH",
  "121": "CRP",
  "125": "ID",
  "219": "GENE",
  "230": "PHYS",
  "232": "SOC",
  "233": "PSY",
  "234": "CHEM",
  "236": "MATH",
  "238": "BIOL",
  "240": "HIST",
  "241": "PHIL",
  "246": "STAT",
  "310": "ADM",
  "311": "ECON",
  "312": "BA",
  "314": "IR",
  "411": "ECE",
  "412": "ESME",
  "413": "EME",
  "421": "PHYE",
  "422": "CHME",
  "423": "MATE",
  "430": "CEIT",
  "450": "FLE",
  "453": "PHED",
  "454": "EDS",
  "560": "ENVE",
  "561": "ES",
  "562": "CE",
  "563": "CHE",
  "564": "GEOE",
  "565": "MINE",
  "566": "PETE",
  "567": "EE",
  "568": "IE",
  "569": "ME",
  "570": "METE",
  "571": "CENG",
  "572": "AEE",
  "573": "FDE",
  "603": "FREN",
  "604": "GERM",
  "605": "JA",
  "606": "ITAL",
  "607": "RUS",
  "608": "SPAN",
  "609": "HEB",
  "610": "GRE",
  "611": "CHN",
  "612": "PERS",
  "639": "ENG",
  "642": "TURK",
  "651": "THEA",
  "909": "MMI",
  "973": "FM",
  "877": "OHS",
};

export function formatMetuCourseCode(value: string) {
  const normalized = value.trim().toUpperCase();
  if (!/^\d{7}$/.test(normalized)) return normalized;
  const abbreviation = DEPARTMENT_ABBREVIATIONS[normalized.slice(0, 3)];
  if (!abbreviation) return normalized;
  const number = normalized.slice(3).replace(/^0+/, "") || "0";
  return `${abbreviation} ${number}`;
}
