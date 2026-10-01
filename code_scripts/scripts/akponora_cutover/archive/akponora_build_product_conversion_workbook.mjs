import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const outputDir = path.resolve(
  process.argv[2] || "outputs/019f8fd0-de6d-70f0-9526-2d79eaddbc52/akponora_phase1",
);
const conversionRows = JSON.parse(await fs.readFile(path.join(outputDir, "conversion_rows.json"), "utf8"));
const qboRows = JSON.parse(await fs.readFile(path.join(outputDir, "qbo_rows.json"), "utf8"));
const comparisonRows = JSON.parse(await fs.readFile(path.join(outputDir, "comparison_rows.json"), "utf8"));
const summary = JSON.parse(await fs.readFile(path.join(outputDir, "summary.json"), "utf8"));

const workbook = Workbook.create();
const summarySheet = workbook.worksheets.add("Summary");
const conversionSheet = workbook.worksheets.add("Product Conversion");
const exceptionsSheet = workbook.worksheets.add("Staff Exceptions");
const changesSheet = workbook.worksheets.add("July-Aug Change");
const qboSheet = workbook.worksheets.add("QBO Legacy");
const sourcesSheet = workbook.worksheets.add("Sources");
const checksSheet = workbook.worksheets.add("Checks");

const navy = "#17365D";
const blue = "#1F4E78";
const paleBlue = "#D9EAF7";
const lightBlue = "#EAF3F8";
const paleYellow = "#FFF2CC";
const paleRed = "#FCE4D6";
const paleGreen = "#E2F0D9";
const grey = "#E7E6E6";
const darkGrey = "#595959";
const white = "#FFFFFF";
const moneyFormat = '₦#,##0.00;[Red](₦#,##0.00);-';
const countFormat = '#,##0.00;[Red](#,##0.00);-';
const integerFormat = '#,##0;[Red](#,##0);-';

function styleTitle(sheet, range, title) {
  sheet.mergeCells(range);
  const titleRange = sheet.getRange(range);
  titleRange.values = [[title]];
  titleRange.format = {
    fill: navy,
    font: { bold: true, color: white, size: 16 },
    verticalAlignment: "center",
  };
  titleRange.format.rowHeight = 30;
}

function styleHeader(range) {
  range.format = {
    fill: blue,
    font: { bold: true, color: white },
    wrapText: true,
    verticalAlignment: "center",
    borders: { preset: "outside", style: "thin", color: "#A6A6A6" },
  };
  range.format.rowHeight = 34;
}

function setWidths(sheet, widths) {
  for (const [column, width] of Object.entries(widths)) {
    sheet.getRange(`${column}:${column}`).format.columnWidth = width;
  }
}

function recommendation(row) {
  const issues = String(row["Issue Codes"] || "");
  if (issues.includes("NEGATIVE_STOCK")) return "Confirm count and correct EPOS stock before approval";
  if (issues.includes("MISSING_COST_WITH_POSITIVE_STOCK")) return "Verify cost from a recent supplier invoice";
  if (issues.includes("NAME_DESCRIPTION_MULTIPLIER_CONFLICT")) return "Inspect physical packaging and correct the wrong EPOS field";
  if (issues.includes("NESTED_PACK_REVIEW")) return "Confirm outer, retail-pack and individual-unit relationships";
  if (issues.includes("CONTENT_VS_STOCK_UNIT_REVIEW")) return "Confirm whether inner pieces are sold separately or are only pack content";
  if (issues.includes("DUPLICATE_PRODUCT_NAME")) return "Identify the correct EPOS product record and retire/rename duplicates";
  if (issues.includes("MULTIPLE_QBO_EXACT_NAME_MATCHES")) return "Select the correct legacy QBO item; do not auto-map";
  return "Review the listed issue and record an approved decision";
}

// Summary
summarySheet.showGridLines = false;
styleTitle(summarySheet, "A1:H1", "AKPONORA Product Conversion & Cutover Review");
summarySheet.getRange("A2:H2").merge();
summarySheet.getRange("A2").values = [[
  `Provisional working file refreshed from the ${summary.as_of} EPOS exports and compared with the ${summary.baseline_as_of} baseline. It is safe for review only and is not approved for QBO import or production pipeline use.`,
]];
summarySheet.getRange("A2:H2").format = { fill: paleYellow, font: { bold: true, color: "#7F6000" }, wrapText: true };
summarySheet.getRange("A2:H2").format.rowHeight = 40;
summarySheet.getRange("A4:B8").values = [
  ["Source as-of", summary.as_of],
  ["Workbook built", summary.built_on],
  ["Target operational restart", "2026-09-01"],
  ["QBO write status", "PAUSED — no writes authorised"],
  ["Current purpose", "Staff review, mapping preparation and cutover readiness"],
];
summarySheet.getRange("A4:A8").format = { fill: grey, font: { bold: true } };
summarySheet.getRange("B4:B8").format = { fill: lightBlue };

summarySheet.getRange("A10:D10").values = [["Readiness metric", "Current result", "Meaning", "Action"]];
styleHeader(summarySheet.getRange("A10:D10"));
const conversionEnd = 5 + conversionRows.length;
const exceptionPriority = (row) => {
  const name = String(row["EPOS Name"] || "").toUpperCase();
  const issues = String(row["Issue Codes"] || "");
  if (name.includes("BACKWOODS")) return 0;
  if (issues.includes("NESTED_PACK_REVIEW")) return 1;
  if (issues.includes("NAME_DESCRIPTION_MULTIPLIER_CONFLICT")) return 2;
  if (issues.includes("MISSING_COST_WITH_POSITIVE_STOCK")) return 3;
  if (issues.includes("NEGATIVE_STOCK")) return 4;
  return 5;
};
const exceptionRows = conversionRows
  .filter((row) => row["Pipeline Status"] === "BLOCK")
  .sort((a, b) => exceptionPriority(a) - exceptionPriority(b) || String(a["EPOS Name"]).localeCompare(String(b["EPOS Name"])));
const exceptionEnd = 5 + exceptionRows.length;
const qboEnd = 5 + qboRows.length;
const changedRows = comparisonRows.filter((row) => row["Classification"] !== "UNCHANGED");
const changeEnd = 5 + changedRows.length;
summarySheet.getRange("A11:A22").values = [
  ["EPOS product rows loaded"],
  ["Products blocked for staff decision"],
  ["Provisional mapping candidates"],
  ["Negative-stock rows"],
  ["Positive-stock rows missing cost"],
  ["Pack-structure review rows"],
  ["Name/Description multiplier conflicts"],
  ["Rows using a shared barcode"],
  ["Exact-name QBO matches"],
  ["EPOS Product IDs recovered"],
  ["EPOS source SKUs present"],
  ["Legacy QBO items with negative quantity"],
];
summarySheet.getRange("B11:B13").formulas = [
  [`=COUNTA('Product Conversion'!$A$6:$A$${conversionEnd})`],
  [`=COUNTIF('Product Conversion'!$B$6:$B$${conversionEnd},"BLOCK")`],
  [`=COUNTIF('Product Conversion'!$B$6:$B$${conversionEnd},"PROVISIONAL")`],
];
summarySheet.getRange("B14:B21").values = [[summary.negative_stock_rows], [summary.missing_cost_positive_stock_rows], [summary.nested_pack_rows], [summary.name_description_conflicts], [summary.shared_barcode_rows], [summary.exact_qbo_matches], [summary.source_product_ids_present], [summary.source_skus_present]];
summarySheet.getRange("B22").formulas = [[`=COUNTIF('QBO Legacy'!$B$6:$B$${qboEnd},"<0")`]];
summarySheet.getRange("C11:D22").values = [
  ["Full Product List snapshot", "Refresh after final EPOS corrections"],
  ["Must not enter the pipeline", "Staff reviews only these rows"],
  ["Candidate mappings, not approvals", "Refresh and revalidate before cutover"],
  ["Count or setup remains unreliable", "Confirm and correct in EPOS"],
  ["Inventory cannot be valued safely", "Verify supplier cost"],
  ["Product names do not prove the stock unit", "Confirm pack/master-product setup"],
  ["EPOS fields contradict one another", "Correct the wrong field"],
  ["Barcode is not a safe mapping key", "Use the generated family SKU and EPOS ID"],
  ["Diagnostic only", "Legacy inventory items remain preserved"],
  ["Recovered from Daily Sales for actively sold products", "Use ID as primary mapping key where present"],
  ["Product List OrderCode and ArticleCode are empty", "Use approved generated family SKU"],
  ["Evidence of historical FIFO distortion", "Preserve; exclude from future stock automation"],
];
summarySheet.getRange("A11:D22").format.borders = { preset: "inside", style: "thin", color: "#D9E2F3" };
summarySheet.getRange("B11:B22").format.numberFormat = integerFormat;

summarySheet.getRange("E10:H10").values = [["July-to-August change", "Count", "Meaning", "Action"]];
styleHeader(summarySheet.getRange("E10:H10"));
summarySheet.getRange("E11:H15").values = [
  ["Negative stock resolved", summary.negative_stock_resolved, "Was negative in July; now zero or positive", "Retain evidence and spot-check"],
  ["New negative stock", summary.new_negative_stock, "Was not negative in July; now negative", "Correct or quarantine"],
  ["Changed products", summary.changed_products, "Stock or cost changed", "Use change sheet for review"],
  ["New products", summary.new_products, "Not present in July Product List", "Confirm setup before approval"],
  ["Removed products", summary.removed_products, "Present in July but absent now", "Confirm archived/retired intentionally"],
];
summarySheet.getRange("F11:F15").format.numberFormat = integerFormat;
summarySheet.getRange("E11:H15").format.borders = { preset: "inside", style: "thin", color: "#D9E2F3" };

summarySheet.getRange("A24:D24").values = [["Staff workflow", "Owner", "Where", "Completion test"]];
styleHeader(summarySheet.getRange("A24:D24"));
summarySheet.getRange("A25:D29").values = [
  ["Review only blocked products", "Stock/product staff", "Staff Exceptions", "Decision and note recorded"],
  ["Confirm pack and master-product relationships", "EPOS administrator", "EPOS + Staff Exceptions", "Canonical unit and multipliers approved"],
  ["Verify missing/uncertain costs", "Purchasing / accountant", "Supplier invoices", "Cost source recorded"],
  ["Export refreshed post-count EPOS files", "EPOS administrator", "COGS Analysis refresh folder", "Product, Stock Management and Stock Levels exports received"],
  ["Approve accounting cutover", "Accountant", "QBO reconciliation phase", "Opening value and journals approved"],
];
summarySheet.getRange("A25:D29").format.wrapText = true;

summarySheet.getRange("A32:D32").values = [["Colour legend", "Meaning", "Editable?", "Rule"]];
styleHeader(summarySheet.getRange("A32:D32"));
summarySheet.getRange("A33:D36").values = [
  ["Yellow input", "Staff decision or verification needed", "Yes", "Complete only from EPOS/physical evidence"],
  ["Blue text", "User-controlled approval input", "Yes", "Do not overwrite formulas"],
  ["Red exception", "Blocked or failed check", "No", "Must be resolved or explicitly approved"],
  ["Green status", "Check passed", "No", "Still not production approval"],
];
summarySheet.getRange("A33:A33").format.fill = paleYellow;
summarySheet.getRange("A34:A34").format.font = { color: "#0000FF" };
summarySheet.getRange("A35:A35").format.fill = paleRed;
summarySheet.getRange("A36:A36").format.fill = paleGreen;
setWidths(summarySheet, { A: 34, B: 34, C: 43, D: 46, E: 28, F: 13, G: 34, H: 36 });
summarySheet.freezePanes.freezeRows(2);

// Product Conversion
conversionSheet.showGridLines = false;
styleTitle(conversionSheet, "A1:AX1", `Product Conversion List — Provisional ${summary.as_of} Snapshot`);
conversionSheet.getRange("A2:AX2").merge();
conversionSheet.getRange("A2").values = [[
  "Rows marked BLOCK need a staff decision. The Product List contains no Product ID or SKU; Daily Sales supplies IDs only for products sold in its report period. Generated AKP SKUs remain candidates until approved. Do not use this sheet in production until refreshed and approved.",
]];
conversionSheet.getRange("A2:AX2").format = { fill: paleYellow, font: { bold: true, color: "#7F6000" }, wrapText: true };
conversionSheet.getRange("A2:AX2").format.rowHeight = 36;
const conversionHeaders = [
  "Row ID", "Pipeline Status", "Review Status", "EPOS Name", "EPOS Description", "Category", "Barcode",
  "EPOS Product ID", "EPOS Existing SKU", "Family Candidate", "Canonical Family SKU", "Stock Tracked",
  "Sell on Till", "EPOS Current Stock", "EPOS Current Volume", "EPOS Total Stock", "EPOS Volume Denominator",
  "EPOS Cost Inc Tax", "Name Trailing Multiplier", "Description Trailing Multiplier", "Proposed Canonical Unit",
  "Proposed Full Unit Multiplier", "Proposed Volume Multiplier", "Provisional Canonical Quantity", "Issue Codes",
  "Staff Decision Needed", "QBO Exact Match", "QBO Product Name", "QBO Item Type", "QBO SKU", "QBO Qty On Hand",
  "QBO Cost", "QBO Income Account", "QBO Expense Account", "QBO Inventory Asset Account", "Target QBO Item Type",
  "Target QBO Name", "Target QBO SKU", "Staff Approved Canonical Unit", "Staff Approved Full Multiplier",
  "Staff Approved Volume Multiplier", "Verified Cost per Full Unit", "Verified Cost per Canonical Unit", "Cost Source",
  "Owner", "Review Notes", "Source Product File", "Staff Approved Sale Multiplier", "Effective Date", "Approved By",
];
conversionSheet.getRange("A5:AX5").values = [conversionHeaders];
styleHeader(conversionSheet.getRange("A5:AX5"));
const conversionValues = conversionRows.map((row) => [
  row["Row ID"], row["Pipeline Status"], row["Review Status"], row["EPOS Name"], row["EPOS Description"],
  row["Category"], row["Barcode"], row["EPOS Product ID"], row["EPOS Existing SKU"], row["Family Candidate"],
  row["Canonical Family SKU"], row["Stock Tracked"], row["Sell on Till"], row["EPOS Current Stock"],
  row["EPOS Current Volume"], row["EPOS Total Stock"], row["EPOS Volume Denominator"], row["EPOS Cost Inc Tax"],
  row["Name Trailing Multiplier"], row["Description Trailing Multiplier"], row["Proposed Canonical Unit"],
  row["Proposed Full Unit Multiplier"], row["Proposed Volume Multiplier"], "", row["Issue Codes"],
  row["Staff Decision Needed"], row["QBO Exact Match"], row["QBO Product Name"], row["QBO Item Type"],
  row["QBO SKU"], row["QBO Qty on Hand"], row["QBO Cost"], row["QBO Income Account"], row["QBO Expense Account"],
  row["QBO Inventory Asset Account"], row["Target QBO Item Type"], row["Target QBO Name"], row["Target QBO SKU"],
  row["Staff Approved Canonical Unit"], row["Staff Approved Full Multiplier"], row["Staff Approved Volume Multiplier"],
  row["Verified Cost per Full Unit"], "", row["Cost Source"], row["Owner"], row["Review Notes"], row["Source Product File"],
  row["Staff Approved Sale Multiplier"], row["Effective Date"], row["Approved By"],
]);
conversionSheet.getRange(`A6:AX${conversionEnd}`).values = conversionValues;
conversionSheet.getRange("X6").formulas = [[`=IF(B6="BLOCK","",IF(OR(V6="",N6=""),"",N6*V6+IF(O6="",0,O6*W6)))`]];
conversionSheet.getRange(`X6:X${conversionEnd}`).fillDown();
conversionSheet.getRange("AQ6").formulas = [[`=IF(OR(AN6="",AP6=""),"",AP6/AN6)`]];
conversionSheet.getRange(`AQ6:AQ${conversionEnd}`).fillDown();
conversionSheet.getRange(`C6:C${conversionEnd}`).dataValidation = {
  rule: { type: "list", values: ["Provisional", "Needs review", "Approved", "Blocked", "Retired"] },
};
conversionSheet.getRange(`AM6:AM${conversionEnd}`).dataValidation = {
  rule: { type: "list", values: ["Each", "Retail pack", "Bottle", "Can", "Sachet", "Other"] },
};
conversionSheet.getRange(`B6:B${conversionEnd}`).conditionalFormats.add("containsText", { text: "BLOCK", format: { fill: paleRed, font: { bold: true, color: "#9C0006" } } });
conversionSheet.getRange(`B6:B${conversionEnd}`).conditionalFormats.add("containsText", { text: "PROVISIONAL", format: { fill: paleYellow, font: { color: "#7F6000" } } });
conversionSheet.getRange(`C6:C${conversionEnd}`).conditionalFormats.add("containsText", { text: "Approved", format: { fill: paleGreen, font: { color: "#006100" } } });
conversionSheet.getRange(`AM6:AT${conversionEnd}`).format = { fill: paleYellow, font: { color: "#0000FF" } };
conversionSheet.getRange(`AV6:AX${conversionEnd}`).format = { fill: paleYellow, font: { color: "#0000FF" } };
conversionSheet.getRange(`N6:Q${conversionEnd}`).format.numberFormat = countFormat;
conversionSheet.getRange(`R6:R${conversionEnd}`).format.numberFormat = moneyFormat;
conversionSheet.getRange(`S6:X${conversionEnd}`).format.numberFormat = countFormat;
conversionSheet.getRange(`AE6:AF${conversionEnd}`).format.numberFormat = moneyFormat;
conversionSheet.getRange(`AN6:AQ${conversionEnd}`).format.numberFormat = moneyFormat;
conversionSheet.getRange(`AW6:AW${conversionEnd}`).format.numberFormat = "yyyy-mm-dd";
conversionSheet.getRange(`D6:AX${conversionEnd}`).format.wrapText = false;
conversionSheet.tables.add(`A5:AX${conversionEnd}`, true, "ProductConversionTable").style = "TableStyleMedium2";
conversionSheet.freezePanes.freezeRows(5);
conversionSheet.freezePanes.freezeColumns(4);
setWidths(conversionSheet, {
  A: 9, B: 13, C: 14, D: 42, E: 42, F: 28, G: 20, H: 15, I: 15, J: 40, K: 18,
  L: 12, M: 12, N: 14, O: 14, P: 14, Q: 15, R: 16, S: 13, T: 13, U: 24, V: 13, W: 13,
  X: 16, Y: 36, Z: 48, AA: 12, AB: 40, AC: 14, AD: 15, AE: 14, AF: 14, AG: 34, AH: 34,
  AI: 30, AJ: 15, AK: 40, AL: 18, AM: 22, AN: 14, AO: 14, AP: 16, AQ: 16, AR: 26, AS: 16,
  AT: 42, AU: 22, AV: 17, AW: 15, AX: 20,
});

// Staff Exceptions
exceptionsSheet.showGridLines = false;
styleTitle(exceptionsSheet, "A1:Q1", "Staff Exception Review — Action Required");
exceptionsSheet.getRange("A2:Q2").merge();
exceptionsSheet.getRange("A2").values = [[
  "This is the only product list staff should review manually. Record a decision, owner and evidence note; product changes themselves must be made in EPOS and then re-exported.",
]];
exceptionsSheet.getRange("A2:Q2").format = { fill: paleYellow, font: { bold: true, color: "#7F6000" }, wrapText: true };
exceptionsSheet.getRange("A2:Q2").format.rowHeight = 36;
const exceptionHeaders = [
  "Priority", "EPOS Name", "Category", "Issue Codes", "Current Stock", "Current Volume", "Total Stock", "Cost Inc Tax",
  "Name Multiplier", "Description Multiplier", "EPOS Volume Denominator", "Decision Needed", "Recommended Action",
  "Owner", "Decision", "Evidence / Notes", "Source Row ID",
];
exceptionsSheet.getRange("A5:Q5").values = [exceptionHeaders];
styleHeader(exceptionsSheet.getRange("A5:Q5"));
const exceptionValues = exceptionRows.map((row) => {
  const issue = String(row["Issue Codes"] || "");
  const critical = /NEGATIVE_STOCK|MISSING_COST_WITH_POSITIVE_STOCK|NAME_DESCRIPTION_MULTIPLIER_CONFLICT|NESTED_PACK_REVIEW/.test(issue);
  return [
    critical ? "Critical" : "High", row["EPOS Name"], row["Category"], issue, row["EPOS Current Stock"],
    row["EPOS Current Volume"], row["EPOS Total Stock"], row["EPOS Cost Inc Tax"], row["Name Trailing Multiplier"],
    row["Description Trailing Multiplier"], row["EPOS Volume Denominator"], row["Staff Decision Needed"],
    recommendation(row), row["Owner"], "Pending", row["Review Notes"], row["Row ID"],
  ];
});
exceptionsSheet.getRange(`A6:Q${exceptionEnd}`).values = exceptionValues;
exceptionsSheet.getRange(`N6:P${exceptionEnd}`).format = { fill: paleYellow, font: { color: "#0000FF" } };
exceptionsSheet.getRange(`O6:O${exceptionEnd}`).dataValidation = {
  rule: { type: "list", values: ["Pending", "Approved", "Correct in EPOS", "Block/Retire", "Not Sold"] },
};
exceptionsSheet.getRange(`A6:A${exceptionEnd}`).conditionalFormats.add("containsText", { text: "Critical", format: { fill: paleRed, font: { bold: true, color: "#9C0006" } } });
exceptionsSheet.getRange(`O6:O${exceptionEnd}`).conditionalFormats.add("containsText", { text: "Approved", format: { fill: paleGreen, font: { color: "#006100" } } });
exceptionsSheet.getRange(`E6:K${exceptionEnd}`).format.numberFormat = countFormat;
exceptionsSheet.getRange(`H6:H${exceptionEnd}`).format.numberFormat = moneyFormat;
exceptionsSheet.getRange(`B6:P${exceptionEnd}`).format.wrapText = true;
exceptionsSheet.tables.add(`A5:Q${exceptionEnd}`, true, "StaffExceptionsTable").style = "TableStyleMedium2";
exceptionsSheet.freezePanes.freezeRows(5);
exceptionsSheet.freezePanes.freezeColumns(2);
setWidths(exceptionsSheet, { A: 12, B: 46, C: 28, D: 40, E: 13, F: 13, G: 13, H: 15, I: 13, J: 13, K: 15, L: 48, M: 48, N: 18, O: 18, P: 50, Q: 12 });

// July to August comparison
changesSheet.showGridLines = false;
styleTitle(changesSheet, "A1:K1", `EPOS Product Changes — ${summary.baseline_as_of} to ${summary.as_of}`);
changesSheet.getRange("A2:K2").merge();
changesSheet.getRange("A2").values = [[
  "Only added, removed, changed or non-comparable products are shown. A resolved negative is progress; a new negative remains blocked. Cost and stock changes should be checked against the count and supplier evidence.",
]];
changesSheet.getRange("A2:K2").format = { fill: paleYellow, font: { bold: true, color: "#7F6000" }, wrapText: true };
changesSheet.getRange("A2:K2").format.rowHeight = 38;
const changeHeaders = [
  "EPOS Name", "Category", "July Total Stock", "August Total Stock", "Stock Change",
  "July Cost Inc Tax", "August Cost Inc Tax", "Cost Change", "Classification", "Pipeline Status", "Current Issue Codes",
];
changesSheet.getRange("A5:K5").values = [changeHeaders];
styleHeader(changesSheet.getRange("A5:K5"));
changesSheet.getRange(`A6:K${changeEnd}`).values = changedRows.map((row) => [
  row["EPOS Name"], row["Category"], row["Baseline Total Stock"], row["Current Total Stock"], row["Stock Change"],
  row["Baseline Cost Inc Tax"], row["Current Cost Inc Tax"], row["Cost Change"], row["Classification"],
  row["Current Pipeline Status"], row["Current Issue Codes"],
]);
changesSheet.getRange(`C6:E${changeEnd}`).format.numberFormat = countFormat;
changesSheet.getRange(`F6:H${changeEnd}`).format.numberFormat = moneyFormat;
changesSheet.getRange(`I6:I${changeEnd}`).conditionalFormats.add("containsText", { text: "NEW_NEGATIVE", format: { fill: paleRed, font: { bold: true, color: "#9C0006" } } });
changesSheet.getRange(`I6:I${changeEnd}`).conditionalFormats.add("containsText", { text: "NEGATIVE_RESOLVED", format: { fill: paleGreen, font: { color: "#006100" } } });
changesSheet.getRange(`I6:I${changeEnd}`).conditionalFormats.add("containsText", { text: "NEW_PRODUCT", format: { fill: paleBlue, font: { color: navy } } });
changesSheet.getRange(`I6:I${changeEnd}`).conditionalFormats.add("containsText", { text: "REMOVED_PRODUCT", format: { fill: grey, font: { color: darkGrey } } });
changesSheet.getRange(`J6:J${changeEnd}`).conditionalFormats.add("containsText", { text: "BLOCK", format: { fill: paleRed, font: { color: "#9C0006" } } });
changesSheet.tables.add(`A5:K${changeEnd}`, true, "JulyAugustChangesTable").style = "TableStyleMedium2";
changesSheet.freezePanes.freezeRows(5);
changesSheet.freezePanes.freezeColumns(2);
setWidths(changesSheet, { A: 48, B: 28, C: 16, D: 16, E: 15, F: 17, G: 17, H: 15, I: 22, J: 16, K: 48 });

// QBO Legacy
qboSheet.showGridLines = false;
styleTitle(qboSheet, "A1:L1", "QBO Legacy Product Snapshot — Preserve, Do Not Repair In Place");
qboSheet.getRange("A2:L2").merge();
qboSheet.getRange("A2").values = [[
  "This sheet records the legacy QBO catalogue as of 23 July. Negative quantities are diagnostic evidence. Existing items and transactions are to be preserved and excluded from the proposed future EPOS-led stock mode.",
]];
qboSheet.getRange("A2:L2").format = { fill: paleYellow, font: { bold: true, color: "#7F6000" }, wrapText: true };
qboSheet.getRange("A2:L2").format.rowHeight = 38;
const qboHeaders = ["QBO Product Name", "Quantity on Hand", "Item Type", "Category", "SKU", "Price", "Cost", "Income Account", "Expense Account", "Inventory Asset Account", "Negative Qty", "Proposed Treatment"];
qboSheet.getRange("A5:L5").values = [qboHeaders];
styleHeader(qboSheet.getRange("A5:L5"));
qboSheet.getRange(`A6:L${qboEnd}`).values = qboRows.map((row) => [
  row["QBO Product Name"], row["Quantity on Hand"], row["Item Type"], row["Category"], row["SKU"], row["Price"],
  row["Cost"], row["Income Account"], row["Expense Account"], row["Inventory Asset Account"],
  typeof row["Quantity on Hand"] === "number" && row["Quantity on Hand"] < 0,
  row["Item Type"] === "Inventory" ? "Preserve legacy item; exclude from future inventory automation" : "Review mapping only",
]);
qboSheet.getRange(`B6:B${qboEnd}`).format.numberFormat = countFormat;
qboSheet.getRange(`F6:G${qboEnd}`).format.numberFormat = moneyFormat;
qboSheet.getRange(`K6:K${qboEnd}`).conditionalFormats.add("cellIs", { operator: "equal", formula: "TRUE", format: { fill: paleRed, font: { color: "#9C0006" } } });
qboSheet.tables.add(`A5:L${qboEnd}`, true, "QBOLegacyTable").style = "TableStyleMedium2";
qboSheet.freezePanes.freezeRows(5);
qboSheet.freezePanes.freezeColumns(2);
setWidths(qboSheet, { A: 46, B: 18, C: 16, D: 30, E: 16, F: 14, G: 14, H: 34, I: 34, J: 30, K: 12, L: 52 });

// Sources
sourcesSheet.showGridLines = false;
styleTitle(sourcesSheet, "A1:H1", "Source Register & Refresh Requirements");
sourcesSheet.getRange("A3:H3").values = [["System", "Source", "As-of", "Rows", "Status", "Required Refresh", "Owner", "Path / Notes"]];
styleHeader(sourcesSheet.getRange("A3:H3"));
const evidenceBase = summary.evidence_root;
const qboEvidenceBase = summary.qbo_evidence_root;
const sourceValues = [
  ["EPOS", `Product List (${summary.product_file_count} pages)`, summary.as_of, summary.product_rows, "Loaded", "Final pre-cutover Product List", "EPOS administrator", `${evidenceBase}/EPOS/Product List`],
  ["EPOS", `Stock Management (${summary.stock_management_file_count} pages)`, summary.as_of, summary.stock_management_rows, "Loaded", "Correct remaining blocked stock/cost rows", "EPOS administrator", `${evidenceBase}/EPOS/Stock Management`],
  ["EPOS", "Stock Levels", summary.as_of, summary.stock_level_rows, "Loaded", "Final 31 August roll-forward snapshot", "EPOS administrator", `${evidenceBase}/EPOS/Stock Levels`],
  ["EPOS", "BookKeeping July and August", summary.as_of, 2, "Loaded", "Complete through cutover close", "EPOS administrator", `${evidenceBase}/EPOS/BookKeeping`],
  ["EPOS", "Daily Sales, Stock History and discrepancies", summary.as_of, summary.daily_sales_rows, "Loaded", "Retain IDs and correction evidence", "EPOS administrator", `${evidenceBase}/EPOS/Extra and Stock History`],
  ["QBO", "Products and Services", summary.baseline_as_of, summary.qbo_product_rows, "Loaded — July baseline", "Fresh 31 August catalogue", "Bookkeeper", `${qboEvidenceBase}/QBO/Products and Services List`],
  ["QBO", "Transaction Detail by Account", "Apr–23 Jul", 4, "Partial", "January–March and complete July–August", "Bookkeeper", `${qboEvidenceBase}/QBO/Transaction Detail by Account`],
  ["QBO", "Balance Sheet, P&L and valuation reports", summary.baseline_as_of, "", "Loaded — July baseline", "Fresh reports through 31 August", "Bookkeeper", `${qboEvidenceBase}/QBO`],
  ["Physical", "Stock count", "Provisional", "", "Mostly complete", "Final dated count or controlled roll-forward", "Stock staff", "Record final included sale/movement date and time"],
  ["Supplier", "Invoices / current costs", "Various", "", "Pending for flagged items", "Recent invoice for uncertain/missing costs", "Purchasing", "Attach invoice reference in Staff Exceptions"],
];
sourcesSheet.getRange("A4:H13").values = sourceValues;
sourcesSheet.getRange("D4:D13").format.numberFormat = integerFormat;
sourcesSheet.getRange("E4:E13").conditionalFormats.add("containsText", { text: "Partial", format: { fill: paleRed, font: { color: "#9C0006" } } });
sourcesSheet.getRange("E4:E13").conditionalFormats.add("containsText", { text: "Pending", format: { fill: paleYellow, font: { color: "#7F6000" } } });
sourcesSheet.getRange("A4:H13").format.wrapText = true;
sourcesSheet.tables.add("A3:H13", true, "SourcesTable").style = "TableStyleMedium2";
sourcesSheet.freezePanes.freezeRows(3);
setWidths(sourcesSheet, { A: 12, B: 38, C: 16, D: 12, E: 20, F: 48, G: 22, H: 80 });

// Checks
checksSheet.showGridLines = false;
styleTitle(checksSheet, "A1:F1", "Workbook Checks");
checksSheet.getRange("A2:B2").values = [["MODEL STATUS", ""]];
checksSheet.getRange("A2").format = { fill: navy, font: { bold: true, color: white } };
checksSheet.getRange("B2").formulas = [[`=IF(COUNTIF(E6:E20,"FAIL")=0,"PASS — READY FOR STAFF REVIEW","FAIL")`]];
checksSheet.getRange("B2").format = { fill: paleGreen, font: { bold: true, color: "#006100" } };
checksSheet.getRange("A4:F4").values = [["Check", "Actual", "Expected", "Delta", "Status", "Where to fix / Notes"]];
styleHeader(checksSheet.getRange("A4:F4"));
const checks = [
  ["Product rows loaded", `='Summary'!B11`, summary.product_rows, "", "", "Source parser / Product Conversion"],
  ["Blocked rows equal exception rows", `='Summary'!B12`, exceptionRows.length, "", "", "Staff Exceptions filter"],
  ["Provisional rows have target SKU", `=COUNTIFS('Product Conversion'!$B$6:$B$${conversionEnd},"PROVISIONAL",'Product Conversion'!$AL$6:$AL$${conversionEnd},"")`, 0, "", "", "Product Conversion target fields"],
  ["Blocked rows do not have target SKU", `=COUNTIFS('Product Conversion'!$B$6:$B$${conversionEnd},"BLOCK",'Product Conversion'!$AL$6:$AL$${conversionEnd},"?*")`, 0, "", "", "Blocked rows must remain quarantined"],
  ["Blank EPOS product names", summary.blank_epos_names, 0, "", "", "Source Product List"],
  ["Generated SKU hash collisions", summary.generated_sku_collisions, 0, "", "", "Canonical SKU generation"],
  ["Source EPOS Product IDs present", summary.source_product_ids_present, summary.product_rows, "", "WARNING", "Daily Sales provides IDs for products sold in its report period"],
  ["Source EPOS SKUs present", summary.source_skus_present, summary.product_rows, "", "WARNING", "Current OrderCode and ArticleCode fields are empty"],
  ["Current EPOS snapshot loaded", 1, 1, "", "OK", "Refresh once more at the final 31 August close"],
  ["January–March QBO transaction reports present", 0, 3, "", "WARNING", "Needed for exact month-by-month historical correction"],
];
checksSheet.getRange("A6:F15").values = checks;
for (let row = 6; row <= 11; row += 1) {
  checksSheet.getRange(`D${row}`).formulas = [[`=B${row}-C${row}`]];
  checksSheet.getRange(`E${row}`).formulas = [[`=IF(D${row}=0,"OK","FAIL")`]];
}
for (let row = 12; row <= 15; row += 1) {
  checksSheet.getRange(`D${row}`).formulas = [[`=B${row}-C${row}`]];
}
checksSheet.getRange("B6:D15").format.numberFormat = integerFormat;
checksSheet.getRange("E6:E15").conditionalFormats.add("containsText", { text: "OK", format: { fill: paleGreen, font: { bold: true, color: "#006100" } } });
checksSheet.getRange("E6:E15").conditionalFormats.add("containsText", { text: "FAIL", format: { fill: paleRed, font: { bold: true, color: "#9C0006" } } });
checksSheet.getRange("E6:E15").conditionalFormats.add("containsText", { text: "WARNING", format: { fill: paleYellow, font: { bold: true, color: "#7F6000" } } });
checksSheet.getRange("A18:F18").merge();
checksSheet.getRange("A18").values = [["PASS means the workbook is internally consistent for staff review. It does not authorise QBO writes or production activation."]];
checksSheet.getRange("A18:F18").format = { fill: paleYellow, font: { bold: true, color: "#7F6000" }, wrapText: true };
checksSheet.getRange("A18:F18").format.rowHeight = 34;
setWidths(checksSheet, { A: 43, B: 16, C: 16, D: 14, E: 18, F: 60 });

// General cleanup and export
for (const sheet of [summarySheet, conversionSheet, exceptionsSheet, changesSheet, qboSheet, sourcesSheet, checksSheet]) {
  const used = sheet.getUsedRange();
  used.format.verticalAlignment = "center";
}

await fs.mkdir(outputDir, { recursive: true });
const previews = [
  ["Summary", "A1:H36", "preview_summary.png"],
  ["Product Conversion", "A1:AX3", "preview_product_conversion_title.png"],
  ["Product Conversion", "A5:M24", "preview_product_conversion.png"],
  ["Product Conversion", "Y5:AX24", "preview_product_conversion_mapping.png"],
  ["Staff Exceptions", "A1:Q24", "preview_staff_exceptions.png"],
  ["July-Aug Change", "A1:K24", "preview_july_aug_change.png"],
  ["QBO Legacy", "A1:L24", "preview_qbo_legacy.png"],
  ["Sources", "A1:H14", "preview_sources.png"],
  ["Checks", "A1:F18", "preview_checks.png"],
];
for (const [sheetName, range, filename] of previews) {
  const preview = await workbook.render({ sheetName, range, scale: 1.2, format: "png" });
  await fs.writeFile(path.join(outputDir, filename), new Uint8Array(await preview.arrayBuffer()));
}

const inspection = await workbook.inspect({
  kind: "table",
  range: "Checks!A1:F18",
  include: "values,formulas",
  tableMaxRows: 20,
  tableMaxCols: 8,
});
await fs.writeFile(path.join(outputDir, "checks_inspection.ndjson"), inspection.ndjson, "utf8");
const errorInspection = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 300 },
  summary: "final formula error scan",
});
await fs.writeFile(path.join(outputDir, "formula_errors.ndjson"), errorInspection.ndjson, "utf8");

const output = await SpreadsheetFile.exportXlsx(workbook);
const workbookName = `akponora_product_conversion_${summary.as_of}.xlsx`;
await output.save(path.join(outputDir, workbookName));
console.log(JSON.stringify({
  workbook: path.join(outputDir, workbookName),
  conversionRows: conversionRows.length,
  exceptionRows: exceptionRows.length,
  changedRows: changedRows.length,
  qboRows: qboRows.length,
}, null, 2));
