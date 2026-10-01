# The supply chain tranche, from the corpus rather than from theory

The next tranche is `rfqs` → `quotations` → `purchase_orders` → `goods_receipts`
and the 3-way match. It is the tranche the corpus supports best, so it is worth
recording what the data actually looks like before writing a table against it.

Everything below is measured from the spreadsheets, not inferred. Where a field
could not be found in the data, that is said rather than filled in.

---

## 1. What the material workbooks actually contain

Sampled from the `VẬT TƯ <zone>` sheets — 13 zones, one workbook each, the
files the corpus inventory found under "40 files mentioning vật tư".

```
r12: VẬT TƯ KHU HPV
r14: PROJECT NAME: BÃI TRÀM ESTATES
r15: DỰ ÁN:  KHU DU LỊCH SINH THÁI HỒN NGỌC B...
r16: PACKAGE: MEP
r17: GÓI THẦU: CƠ ...
r18: REV: BTE-HBG-MAA-HPV-...
r23: STT | | | | | Mã Hiệu | Tên vật tư | % HT | Ngày yêu cầu vật tư từ CT
                    | Ngày phòng vật tư đặt hàng | Ngày dự kiến hàng về
                    | Ngày thực tế hàng về | Ngày dự hàng về | Ngày thực hàng về thực tế
                    | Ngày phê duyệt | Tháng 9 | Ghi chú
r28: 1 | SSA | | HBG | 2 | BTE-WP4-HBC-SHD-MEP-HVAC-HVA-BPV-001 | Ống đồng và phụ kiện
```

### Four things this changes

**1. There is no 12-character material code.** Tập 1 §4.3 specifies one —
`[Hệ]-[Nhóm]-[Loại]-[Số]` across PW/LV/WD/AC/FP — and a regex scan of the corpus
for code-shaped strings returned **zero matches**. What exists instead is
`BTE-WP4-HBC-SHD-MEP-HVAC-HVA-BPV-001`, which is a *drawing* number
(shop drawing, HVAC, valve, BPV zone), and it is what the workbook calls
`Mã Hiệu`.

So the material master has two codes, and conflating them is a mistake worth
naming:

| Field | Shape | What it is |
|---|---|---|
| `code` | 12 chars, Tập 1's scheme | the **material** code, which the corpus does not yet contain |
| `drawing_ref` | `BTE-WP4-HBC-SHD-MEP-...` | the **drawing** the material is detailed on |

The second is a foreign key to a `drawings` table, not a material attribute. A
material appears on many drawings and a drawing covers many materials, so storing
the drawing number *on* the material would be wrong — it belongs on the line that
requests the material, which is where the corpus has it.

**2. "Mã Hiệu" is ambiguous in the source and must not be.** Columns 2–5 of a data
row read `SSA | | HBG | 2` under a header row that labels only `STT`, `Mã Hiệu`
and `Tên vật tư`. The sub-header is not aligned with the columns, so what `SSA`,
`HBG` and `2` mean is not recoverable from the file. Three values, three
unlabelled columns, one of them a plausible supplier code and one a plausible
system code.

A parser that maps position to meaning would be inventing. The ingest must read
`Mã Hiệu` by **header text**, not by column index, and leave unlabelled columns
in a `raw_cells` JSON column for a human. This is the same class of problem as
the `foxz` sheet: the file's layout is not a contract.

**3. The dates are the real content, and there are seven of them.** A material
line tracks: requested by the client, ordered by the procurement office, expected
in, actually in, expected again, actually in again, and approved. That is a
**timeline, not a set of columns**, and it is the reason `material_reconciliations`
has to exist: six dates per line across 13 zones is a lot of state to answer the
question the sheet is actually asking, which is "is this late and by how much".

Tập 1 §2.2 puts Design Freeze / Major PO at G3, and G3's exit criteria include
"PO chính trong ngân sách" and "không NCC ngoài danh mục phê duyệt". The date
timeline is how a PO is known to be on time, and a Gate that checks it needs the
timeline, not a single `expected_date`.

**4. `#REF!` is in the data.** Row 28's `Ngày phê duyệt` cell is `#REF!` — a
broken formula. `EXCEL_ERRORS` in `ingest/numbers.py` already refuses these by
name, and the refusal reason distinguishes an error cell from a number so a
missing date is not a zero. Ten thousand material lines will contain a few, and
each one must not become "approved on the 1st".

---

## 2. The 3-way match, from the form

Tập 3 §1.5, `FRM-BO-002A`, transcribed. This is the shape the finance tables have
to support, and it is more specific than "compare three numbers":

| Block | Fields |
|---|---|
| **Reference** | Auto-generated request number; project & cost code; supplier (**only from the approved list, with its rating shown**); PO number; GRN number; e-invoice number |
| **3-way match** (system-filled) | PO / GRN / Invoice values; variance % and value; within or outside tolerance; **reason code if it varies — Price / Quantity / Quality / Document** |
| **Contract terms** | Retention withheld __%; guarantee valid until __; quality documents attached (**for subcontractors: QA/QC acceptance + HSE sign-off**) |
| **Approval** | Amount requested; approval level routed by DOA; **the approver sees the whole basis on one screen**; e-signature |
| **After payment** | Bank payment reference; MISA posting date; **executor (≠ approver)**; payable status updated |

Three constraints in that table are not obvious and all three are enforceable:

**The reason code is required when there is a variance.** A 3% variance and a 30%
variance are both "outside tolerance" until someone says *why*, and the reason
determines whether the invoice is paid, queried or rejected. A variance without a
reason code is a variance nobody can action, so the check is
`variance_pct > tolerance_pct → reason_code is not null`.

**The supplier rating travels with the payment.** "hạng hiển thị kèm" — the
supplier's A/B/C classification is displayed with the request. So the rating is
read at payment time, not just at approval time: a supplier downgraded to C after
the contract was signed must change what the approver sees. `supplier_scorecards`
carries the current rating; the payment request snapshots it, because a payment
approved against last month's rating is the audit question.

**The executor is not the approver.** Tập 1 §1.2 again, and here it is a *field on
the form*. `payments.executed_by <> payments.approved_by` as a check constraint,
matching the one already on `gate_decisions`.

**Retention is a computed deduction, not a note.** "Retention giữ lại __%" against
the contract's `retention_pct` means the payment request carries both the
contract rate and the amount actually withheld, and they must agree. A payment
that retains the wrong amount is a contract breach discovered at final account.

---

## 3. What the corpus does not contain, for this tranche

| Needed for | Present? | Consequence |
|---|---|---|
| Material master with 12-char codes | **No** | `materials.code` is nullable until the codes are minted, or seeded from `Mã Hiệu` with `drawing_ref` set and a note |
| Subcontract agreements with quantities | Partial | the one real case file is a purchase of lighting, not a subcontract |
| PO / GRN / invoice as structured records | **No** | the three-way match cannot be *validated* on real data, only unit-tested |
| Supplier qualification files | **Zero** | Supplier Due Diligence remains unvalidatable |
| Retention actually withheld on a payment | **No** | the retention agreement constraint is unproven against history |

So the supply tranche can be **built and unit-tested** but the 3-way match
specifically cannot be **measured**, and I will say so rather than implying
otherwise. It is still the right tranche to build: it is the process the corpus
documents most completely, and a match that has never met real data is better
waiting a month than waiting for a table.

---

## 5. What was built from this — migration `0011`

Five tables, written against the measurements above rather than against a theory
of procurement.

| Table | Holds | The measurement that shaped it |
|---|---|---|
| `material_categories` | Self-referential classification tree | MEP supply does not fit a fixed level count; a cable is under electrical/cable/power |
| `materials` | A purchasable item | `code` nullable (the corpus has none); partial unique index; `raw_cells` for the unlabelled columns |
| `supplier_documents` | One piece of a DD file | `valid_from`/`valid_to`, not one date — Vietnamese GPKD and ISO certificates expire |
| `supplier_risk_flags` | A named finding with a basis | Tập 1 §5.3's fourth forbidden zone; `is_blocking` makes the freeze a recorded fact |
| `supplier_assessments` | A due-diligence *run* | Tập 3 §1.4's score is a conclusion; a challenge must be able to reproduce it |

### `materials.code` is nullable, and that is the interesting decision

Tập 1 §4.3 specifies a 12-character code, Procurement is the sole owner, and
duplicates are prohibited. A regex scan of 400 workbooks for code-shaped strings
returned **zero**.

So the column is nullable, and the uniqueness index is **partial**:

```sql
CREATE UNIQUE INDEX uq_materials_org_code_when_present
  ON materials (organization_id, code) WHERE code IS NOT NULL
```

Several NULLs coexist; two identical codes do not. That is "cấm tạo mã trùng"
as a constraint rather than as a review step somebody remembers to perform.

The alternative was NOT NULL with a generated code, which would have been worse
twice over: it invents codes the specification reserves for Procurement, and it
makes a code that looks authoritative while being wrong. A NULL is honest; a
generated code is not.

A present code must match the scheme, so a typo cannot create a second numbering
series — the same constraint `sop_definitions.code` carries for the same reason.

### `drawing_ref` is deliberately not on `materials`

The corpus has `Mã Hiệu = BTE-WP4-HBC-SHD-MEP-HVAC-HVA-BPV-001`, a drawing
number, in the material schedule. It is tempting to put it on the material.

It would be wrong. A material appears on many drawings and a drawing covers many
materials, so a single-valued column holding that reference is the first material
detailed on two drawings overwriting the first. The reference belongs on the
requisition line, which is where the corpus has it — and the corpus has it there
because the corpus is a *schedule*, and a schedule is a list of lines.

### `raw_cells` holds what the headers do not name

A data row reads `SSA | | HBG | 2` under a header labelling only `STT`,
`Mã Hiệu` and `Tên vật tư`. The sub-header is not aligned with the columns, so
those three values cannot be mapped to meaning — one looks like a supplier code
and one like a system code, and neither is recoverable from the file.

`materials.raw_cells` keeps them verbatim. A parser that assigned them meanings
would be inventing a supplier code from a spreadsheet layout, which is the failure
mode the whole ingest layer is built to avoid (§6a of `CONSTRUCTION_DOMAIN.md`).
The same reasoning as the `foxz` sheet: the file's layout is not a contract.

## 6. Ordering, and why the material master comes before the RFQ

`materials` and `material_categories` are needed before `rfq_items`, because a
requisition line names a material and Tập 1 §4.3 makes Procurement the sole owner
of material codes — "Cấm tạo mã trùng; Procurement là owner duy nhất". A
platform where two requisitions can invent two codes for one material has
defeated the entire master-data programme, so the uniqueness constraint is on
`(organization_id, code)` and the *drawing* reference is on the line.

The sequence, and the dependency each step has on the previous:

| Step | Tables | Needs first |
|---|---|---|
| 1 | `materials`, `material_categories` | — |
| 2 | `supplier_documents`, `supplier_risk_flags`, `supplier_scorecards` | `suppliers` (tranche 3) |
| 3 | `rfqs`, `rfq_items` | step 1 |
| 4 | `quotations`, `quotation_items` | step 3 |
| 5 | `purchase_orders`, `po_items` | step 4, `contracts` |
| 6 | `goods_receipts`, `receipt_items` | step 5 |
| 7 | `invoices`, `three_way_matches`, `payments` | step 6 |

Step 7 is `ONX-BO-FIN-SOP-002` and is the reason steps 1–6 exist. It goes in its
own tranche because the 3-way match is the one part of this that has a hard
dependency on three prior steps and the one part that cannot be validated.
