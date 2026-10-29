# Interleaved 20 µM base plate

Built from `IDT order_06012026_Ref DNA Book_new source plate.xlsx`, using its four 96-well order worksheets and the interleaving diagram supplied on October 13, 2026.

| IDT source | 384-well rows | 384-well columns | Example |
| --- | --- | --- | --- |
| 1 (blue) | A,C,E,G,I,K,M,O | 01–12 | B01 → C01 |
| 2 (red) | A,C,E,G,I,K,M,O | 13–24 | B01 → C13 |
| 3 (yellow) | B,D,F,H,J,L,N,P | 01–12 | A01 → B01 |
| 4 (green) | B,D,F,H,J,L,N,P | 13–24 | A01 → B13 |

The CSV contains the 252 ordered sequences, including 18 balanced rigid hinges and eight updated staples, plus eight D-Biotin additions in P17–P24 (260 inventory entries). Other unordered wells are omitted. The plate is still `SourcePlate8[8]`, with 20 µM stocks. The original-layout LowConc CSV also inventories the same eight additions in its free P17–P24 wells; its existing staples retain their locations.

`Well Position` is the actual interleaved source location. `Original Well Position` is the logical replacement key used by PAINT sheets; it is not necessarily the previous physical location. Existing sequence matches retain their logical keys. Eight newly ordered sequences identify their logical keys in their names (H05, H08, H11, H14, I05, I08, I11, I14). IDT source plate, source well, and previous quadrant well columns retain provenance for the ordered staples. D-Biotin additions use the exact PAINT P1 sequences, including `/5Biosg/`, and have blank IDT provenance.

## D-Biotin loading map

Load these eight previously empty wells at **20 µM** before using newly prepared RGB picklists. The software inventory describes the intended loaded plate; it does not confirm physical loading.

| LowConc well | D-Biotin staple | Existing PAINT P1 well | Logical base key | Unmodified staple on interleaved plate |
| --- | --- | --- | --- | --- |
| P17 | D-Biotin_H01-01 | B02 | J08 | D08 |
| P18 | D-Biotin_H01-05 | B04 | J04 | D04 |
| P19 | D-Biotin_H01-08 | B06 | J10 | D10 |
| P20 | D-Biotin_H01-12 | B08 | J14 | D14 |
| P21 | D-Biotin_H15-01 | B03 | I08 | G22 |
| P22 | D-Biotin_H15-05 | B05 | I04 | B04 |
| P23 | D-Biotin_H15-08 | B07 | I10 | B10 |
| P24 | D-Biotin_H15-12 | B09 | I14 | G24 |

The existing unmodified staples stay physically present. `Usage=Supplemental` prevents the additions from silently replacing originals in the default manual base mixture. Every RGB run explicitly selects all eight D-Biotin staples and omits their unmodified counterparts. Their target is the independent **Biotin target (nM)** beside the book-base target, separate from both the structural staple and RGB site targets. Existing settings initially inherit the saved book-base target; subsequent edits are independent. This also works when RGB uses the 200 µM base: D-Biotin still comes from the 20 µM LowConc source, with its transfer volume adjusted accordingly.

`lowconc_D_biotin_additions.csv` is the operational source map and sequence list. RGB exposes its source map, source plate, and stock concentration under **Sources & mixing…**. The selected map and all eight staples are recorded in saved batch manifests and stored runs. PAINT P1's original entries remain available for manual workflows. Rebuilding the interleaved inventory appends these additions and rejects any occupied-well collision.

Restart the app and select **LowConc base plate — 20 µM (interleaved)** under **Base plate stock** in RGB Image or Run & Mixing Settings. This selects the ordered rigid hinges. Flexible hinges are absent from this order; choosing them is rejected rather than silently omitting hinges.

Prepare and save a new image batch after selecting this plate. Previously saved picklists retain their old well locations. PAINT replacement plate 7 is unchanged.

To rebuild (requires openpyxl), from the picklist_generator folder:

```sh
python3 scripts/build_interleaved_base.py '/path/to/IDT order_06012026_Ref DNA Book_new source plate.xlsx' replacement_sheets/book_base_20uM.csv replacement_sheets/book_base_20uM_interleaved.csv
```
