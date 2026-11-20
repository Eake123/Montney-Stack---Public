"""Rebuild network/gas/{nodes,areas,links}.geojson from cited public coordinates.

    uv run python network/gas/build.py            # fetches TC's /areas outlines, rewrites the files

Coordinates come from (all cited per feature in `source`):
  * CER Pipeline Profiles key points (CER open data, OGL-Canada)
  * Canadian Geographical Names Database (CGNDB, NRCan, OGL-Canada), by feature id
  * TC Customer Express /areas: the NGTL DOP area outlines and centres
Lines are schematic (drawn node to node through those points), never surveyed routes.
"""
import json
from pathlib import Path

import httpx

OUT = Path(__file__).resolve().parent
TC_AREAS = httpx.get("https://f51561ras5.execute-api.us-west-2.amazonaws.com/production/areas",
                     timeout=60).json()["data"]

CGN = "https://geogratis.gc.ca/services/geoname/en/geonames/{}  (CGNDB, NRCan, OGL-Canada)"
CER = ("CER Pipeline Profiles key point '{}' ({}), "
       "https://github.com/mbradds/pipeline-profiles/blob/master/src/data_management/raw_data/key_points.csv "
       "(CER open data, OGL-Canada)")
GAZ = ("US Census Bureau 2023 Gazetteer, place {} ({}), internal point, "
       "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2023_Gazetteer/2023_Gaz_place_national.zip (public domain)")
TC_AREAS_URL = "https://f51561ras5.execute-api.us-west-2.amazonaws.com/production/areas (TC Customer Express, fetched 2026-10-31)"

# name -> (lat, lon, citation)
P = {
    "empress": (50.9555556, -110.0058333, CGN.format("IACQD") + ": village of Empress"),
    "mcneill": (50.6802778, -110.0277778, CGN.format("IAISW") + ": McNeill"),
    "east_gate": (50.6608, -110.028, CER.format("East Gate", "NGTL")),
    "west_gate": (49.6348, -114.588, CER.format("West Gate", "NGTL")),
    "kingsgate": (49.0015, -116.187, CER.format("Kingsgate", "Foothills")),
    "monchy": (49.0004, -107.544, CER.format("Monchy", "Foothills")),
    "huntingdon": (49.0034, -122.22, CER.format("Huntingdon/FortisBC Lower Mainland", "Westcoast")),
    "kingsvale": (49.9583, -120.916, CER.format("Kingsvale", "Westcoast")),
    "sunset_creek": (55.8136, -120.867, CER.format("Sunset Creek", "Westcoast")),
    "saturn": (55.99558, -121.1104, CER.format("Saturn", "NGTL")),
    "usjr_kp": (51.9475, -114.74, CER.format("Upstream of James River", "NGTL")),
    "calgary_kp": (51.22022, -114.4925, CER.format("Calgary", "NGTL")),
    "edmonton": (53.5344445, -113.4902778, CGN.format("IACMP") + ": Edmonton"),
    "north_east_kp": (53.6374, -112.018, CER.format("North and East", "NGTL")),
    "osda_liege": (56.9473, -111.80979, CER.format("OSDA Liege", "NGTL")),
    # PLAN 4b item 7: NGTL's other export borders (TC 2026 delivery files list them as BORDER stations).
    "ab_mt_border": (49.020973, -113.374409, "AER ST102 Facility List (static.aer.ca/prd/data/codes/ST102-SHP.zip): ABMS0003868 Alberta Montana Border Interco (NOVA); AER, reproduced for personal non-commercial use with attribution"),
    "cold_lake_border": (54.347784, -110.018211, "AER ST102 Facility List (static.aer.ca/prd/data/codes/ST102-SHP.zip): ABMS0001417 Cold Lake Border (NOVA); AER, reproduced for personal non-commercial use with attribution"),
    "unity_border": (52.252118, -110.040745, "AER ST102 Facility List (static.aer.ca/prd/data/codes/ST102-SHP.zip): ABMS0001250 Unity Border (NOVA); AER, reproduced for personal non-commercial use with attribution"),
    "alliance_zone2": (55.8558, -119.591, CER.format("Zone 2", "Alliance")),
    "alliance_border": (49.0, -101.588, CER.format("Border", "Alliance")),
    "welwyn": (50.324691, -101.517583, CGN.format("HAMRK") + ": Welwyn (the TC Mainline's last SK receipt, by the MB "
               "border)"),
    # PLAN 5a map fix: the meter, not the Gordondale place name (14 km off). AER ST102 point (section centre).
    "gordondale": (55.817887, -119.762207, "AER ST102 Facility List (static.aer.ca/prd/data/codes/ST102-SHP.zip): ABMS0003886 Gordondale Interconnection (NOVA), section centre; AER, reproduced for personal non-commercial use with attribution"),
    "groundbirch": (55.783333, -120.91667, CGN.format("JAFZG") + ": Groundbirch"),
    # PLAN 5a map fix: the CGL compressor station where NGTL's Willow Valley Interconnect delivers (was the place name, ~2 km off).
    "wilde_lake": (55.799434, -120.890812, "BCER open data facility locations (data-bc-er.opendata.arcgis.com): facility 00018222 CGLPL WILDE LAKE 04-33-078-19, compressor station, active (BCER Open Data Licence)"),
    "chetwynd": (55.695833, -121.638333, CGN.format("JCPML") + ": Chetwynd"),
    "fort_st_john": (56.2525, -120.846667, CGN.format("JAGPB") + ": Fort St. John"),
    "kitimat": (54.053333, -128.652222, CGN.format("JCMDB") + ": Kitimat (LNG Canada terminal is on the Kitimat waterfront)"),
    "grande_prairie": (55.1708334, -118.7961111, CGN.format("IAHMM") + ": Grande Prairie"),
    "valleyview": (55.065944, -117.281861, CGN.format("IAOBD") + ": Valleyview"),
    "fox_creek": (54.3941666, -116.8066667, CGN.format("IAFHC") + ": Fox Creek"),
    "edson": (53.5836111, -116.4327778, CGN.format("IACMW") + ": Edson"),
    "carbon": (51.4858333, -113.1505556, CGN.format("IARMM") + ": Carbon"),
    "hanna": (51.6458334, -111.9252778, CGN.format("IAINF") + ": Hanna"),
    "suffield": (50.2183333, -111.1611111, CGN.format("IALVQ") + ": Suffield (AECO-C storage hub area)"),
    "fort_sask": (53.7111111, -113.2177778, CGN.format("IAFFM") + ": Fort Saskatchewan"),
    "kerrobert": (51.9216639, -109.1406778, CGN.format("HADHV") + ": Kerrobert"),
    "prince_george": (53.913056, -122.745278, CGN.format("JBLVS") + ": Prince George"),
    "williams_lake": (52.129167, -122.14, CGN.format("JBTMB") + ": Williams Lake"),
    "kamloops": (50.675833, -120.339444, CGN.format("JAFNW") + ": Kamloops"),
    "vanderhoof": (54.0175, -124.0075, CGN.format("JCVCK") + ": Vanderhoof"),
    "burns_lake": (54.230278, -125.764444, CGN.format("JAECH") + ": Burns Lake"),
    "houston": (54.398333, -126.648333, CGN.format("JBANJ") + ": Houston"),
    "dawson_creek": (55.760556, -120.235556, CGN.format("JAPCN") + ": Dawson Creek"),
    # PLAN step 5 PR B: TC Mainline, the rest of Westcoast (vertices are CER key points or CGNDB places; schematic)
    "prairies_kp": (50.6836, -110.087, CER.format("Prairies", "TC Mainline")),
    "moose_jaw": (50.3934, -105.552, CGN.format("HALTS") + ": Moose Jaw"),
    "regina": (50.4548, -104.6066, CGN.format("HAIMP") + ": Regina"),
    "brandon": (49.8469, -99.9522, CGN.format("GADMZ") + ": Brandon"),
    "mainline_nol": (49.718, -96.9859, CER.format("Northern Ontario Line", "TC Mainline")),
    "emerson": (49.0012, -97.0561, CER.format("Emerson II", "TC Mainline")),
    # L-077: the Mainline east along the Northern Ontario Line to North Bay Junction (CGNDB places; schematic)
    "swift_current": (50.2881611, -107.7938944, CGN.format("HAPKT") + ": Swift Current"),
    "winnipeg": (49.884444, -97.146389, CGN.format("GBEIN") + ": Winnipeg"),
    "kenora": (49.8161111, -94.435, CGN.format("FEVJJ") + ": Kenora"),
    "dryden": (49.7833333, -92.7652777, CGN.format("FEVJT") + ": Dryden"),
    "thunder_bay": (48.4013889, -89.2677778, CGN.format("FCWFX") + ": Thunder Bay"),
    "nipigon": (49.011389, -88.266111, CGN.format("FDGEC") + ": Nipigon"),
    "hearst": (49.6869445, -83.6544445, CGN.format("FBMTW") + ": Hearst"),
    "kapuskasing": (49.4152778, -82.4169445, CGN.format("FBTBP") + ": Kapuskasing"),
    "cochrane": (49.1747223, -81.0630555, CGN.format("FEVJK") + ": Cochrane"),
    "nbj": (46.379, -79.4704, CER.format("Eastern Triangle - NOL Receipts", "TC Mainline") + ": compressor station "
            "116 near North Bay, the NOL's east end (North Bay Junction)"),
    "fort_nelson": (58.8056, -122.6972, CGN.format("JAGOO") + ": Fort Nelson"),
    "aitken_creek": (56.952083, -121.921875, "Petrinex BC facility list (PRAFacilityIds.csv): BCMS3164714 Aitken Creek "
                     "Interconnect, NTS B-044-L/094-A-13, converted by NTS arithmetic (build_stations.nts)"),
    "vancouver": (49.2611, -123.1139, CGN.format("JBRIK") + ": Vancouver"),
    # L-085 PacNW (NWP, GTN). US places: Census 2023 Gazetteer internal points (public domain), by GEOID.
    "sumas": (48.995534, -122.268453, GAZ.format("5368330", "Sumas city, WA")),
    "mount_vernon": (48.417071, -122.311832, GAZ.format("5347560", "Mount Vernon city, WA")),
    "snohomish": (47.928866, -122.092684, GAZ.format("5365170", "Snohomish city, WA")),
    "sumner": (47.227215, -122.236264, GAZ.format("5368435", "Sumner city, WA")),
    "jackson_prairie": (46.53914, -122.83578, "WA DNR Oil and Gas Wells (gis.dnr.wa.gov Public_Geology/Oil_and_Gas_Wells "
                        "MapServer/1): median of the 26 Puget Sound Energy / Jackson Prairie Gas Storage wells, Lewis "
                        "County (public)"),
    "washougal": (45.582801, -122.346384, GAZ.format("5376405", "Washougal city, WA")),
    "goldendale": (45.819145, -120.823366, GAZ.format("5327435", "Goldendale city, WA")),
    "stanfield": (45.782259, -119.21613, GAZ.format("4169900", "Stanfield city, OR")),
    "bonners_ferry": (48.693579, -116.317019, GAZ.format("1609370", "Bonners Ferry city, ID")),
    "sandpoint": (48.281694, -116.560013, GAZ.format("1672100", "Sandpoint city, ID")),
    "rathdrum": (47.794036, -116.892927, GAZ.format("1666340", "Rathdrum city, ID")),
    "rosalia": (47.236287, -117.376477, GAZ.format("5359775", "Rosalia town, WA")),
    "starbuck": (46.521599, -118.128876, GAZ.format("5367490", "Starbuck town, WA")),
    "madras": (44.663897, -121.160438, GAZ.format("4145250", "Madras city, OR")),
    "bend": (44.056683, -121.307991, GAZ.format("4105800", "Bend city, OR")),
    "chemult": (43.215056, -121.785013, GAZ.format("4112750", "Chemult CDP, OR")),
    "malin": (42.013367, -121.409744, GAZ.format("4145400", "Malin city, OR")),
    "yakima": (46.590938, -120.5494, GAZ.format("5380010", "Yakima city, WA")),
    # L-090 California import points and the summary anchor (Census 2023 Gazetteer internal points)
    "fresno": (36.782684, -119.793359, GAZ.format("0627000", "Fresno city, CA")),
    # L-094 Rockies (Census 2023 Gazetteer internal points)
    "opal": (41.768055, -110.324078, GAZ.format("5657810", "Opal town, WY")),
    "kemmerer": (41.778484, -110.552817, GAZ.format("5642005", "Kemmerer city, WY")),
    "green_river": (41.512922, -109.470796, GAZ.format("5633740", "Green River city, WY")),
    "salt_lake_city": (40.776928, -111.930991, GAZ.format("4967000", "Salt Lake City city, UT")),
    "beaver_ut": (38.275872, -112.638098, GAZ.format("4904060", "Beaver city, UT")),
    "st_george": (37.077006, -113.576534, GAZ.format("4965330", "St. George city, UT")),
    "las_vegas": (36.233499, -115.264037, GAZ.format("3240000", "Las Vegas city, NV")),
    "wells_nv": (41.113155, -114.953537, GAZ.format("3283000", "Wells city, NV")),
    "winnemucca": (40.96444, -117.724594, GAZ.format("3284800", "Winnemucca city, NV")),
    "soda_springs": (42.657874, -111.586081, GAZ.format("1675195", "Soda Springs city, ID")),
    "pocatello": (42.872748, -112.463836, GAZ.format("1664090", "Pocatello city, ID")),
    "twin_falls": (42.564674, -114.460608, GAZ.format("1682810", "Twin Falls city, ID")),
    "boise": (43.600229, -116.231656, GAZ.format("1608830", "Boise City city, ID")),
    "baker_city": (44.774886, -117.831986, GAZ.format("4103650", "Baker City city, OR")),
    "rangely": (40.087011, -108.78326, GAZ.format("0862880", "Rangely town, CO")),
    "ignacio": (37.118004, -107.637557, GAZ.format("0838535", "Ignacio town, CO")),
    # L-095 Midwest (Census 2023 Gazetteer internal points)
    # L-099: Vector's route (Census 2023 Gazetteer) and Dawn's side of the St. Clair River (CGNDB)
    "highland_in": (41.546622, -87.457616, GAZ.format("1833466", "Highland town, IN")),
    "athens_mi": (42.087394, -85.236217, GAZ.format("2603880", "Athens village, MI")),
    "milford_mi": (42.588539, -83.601648, GAZ.format("2653960", "Milford village, MI")),
    "marine_city": (42.714297, -82.50279, GAZ.format("2651600", "Marine City city, MI")),
    "sault_mi": (46.476169, -84.370544, GAZ.format("2671740", "Sault Ste. Marie city, MI")),
    "dawn": (42.6577778, -82.2275, CGN.format("FAVQD") + ": Dawn Valley (Dawn-Euphemia; Enbridge Gas's Dawn hub)"),
    "courtright": (42.8186111, -82.4688889, CGN.format("FATNI") + ": Courtright (the St. Clair River crossings)"),
    "windsor_on": (42.30227, -82.99348, CGN.format("FDEGT") + ": Windsor (Ojibway, Panhandle's Detroit River "
                   "crossing)"),
    "london_on": (42.9675, -81.2325, CGN.format("FCAEN") + ": London"),
    "kirkwall_on": (43.3588889, -80.1577778, CGN.format("FBUZM") + ": Kirkwall (TC / Enbridge Gas)"),
    "chicago": (41.837045, -87.684939, GAZ.format("1714000", "Chicago city, IL")),
    "joliet": (41.517701, -88.148846, GAZ.format("1738570", "Joliet city, IL")),
    "princeton_il": (41.380565, -89.4641, GAZ.format("1761899", "Princeton city, IL")),
    "sandwich_il": (41.650067, -88.619021, GAZ.format("1767548", "Sandwich city, IL")),
    "davenport": (41.55682, -90.603948, GAZ.format("1919000", "Davenport city, IA")),
    "harper_ia": (41.363721, -92.050895, GAZ.format("1934545", "Harper city, IA")),
    "marshalltown": (42.0343, -92.906266, GAZ.format("1949755", "Marshalltown city, IA")),
    "ventura_ia": (43.128868, -93.465914, GAZ.format("1980580", "Ventura city, IA")),
    "big_rapids": (43.699671, -85.48125, GAZ.format("2608300", "Big Rapids city, MI")),
    "bridgman": (41.935169, -86.56711, GAZ.format("2610580", "Bridgman city, MI")),
    "crystal_falls": (46.090199, -88.321439, GAZ.format("2619140", "Crystal Falls city, MI")),
    "farwell_mi": (43.83684, -84.867335, GAZ.format("2627500", "Farwell village, MI")),
    "gaylord": (45.021342, -84.681822, GAZ.format("2631720", "Gaylord city, MI")),
    "ironwood": (46.452209, -90.150432, GAZ.format("2641060", "Ironwood city, MI")),
    "lansing": (42.714341, -84.560889, GAZ.format("2646000", "Lansing city, MI")),
    "manistique": (45.959744, -86.250953, GAZ.format("2650760", "Manistique city, MI")),
    "richmond_mi": (42.810558, -82.752173, GAZ.format("2668380", "Richmond city, MI")),
    "st_clair_mi": (42.82653, -82.492433, GAZ.format("2670680", "St. Clair city, MI")),
    "st_ignace": (45.887164, -84.731707, GAZ.format("2670840", "St. Ignace city, MI")),
    "bemidji": (47.487034, -94.88269, GAZ.format("2705068", "Bemidji city, MN")),
    "carlton_mn": (46.655769, -92.430397, GAZ.format("2710018", "Carlton city, MN")),
    "chisago_city": (45.345584, -92.910034, GAZ.format("2711350", "Chisago City city, MN")),
    "frazee": (46.728773, -95.703904, GAZ.format("2722472", "Frazee city, MN")),
    "grand_rapids_mn": (47.238518, -93.53257, GAZ.format("2725118", "Grand Rapids city, MN")),
    "hallock": (48.765415, -96.942485, GAZ.format("2726576", "Hallock city, MN")),
    "marshall_mn": (44.449061, -95.789201, GAZ.format("2740688", "Marshall city, MN")),
    "thief_river_falls": (48.111241, -96.177562, GAZ.format("2764570", "Thief River Falls city, MN")),
    "glasgow_mt": (48.199027, -106.632077, GAZ.format("3031075", "Glasgow city, MT")),
    "glen_ullin": (46.811696, -101.833498, GAZ.format("3830860", "Glen Ullin city, ND")),
    "killdeer": (47.368751, -102.746201, GAZ.format("3842700", "Killdeer city, ND")),
    "watford_city": (47.801492, -103.266731, GAZ.format("3883860", "Watford City city, ND")),
    "aberdeen_sd": (45.464678, -98.468104, GAZ.format("4600100", "Aberdeen city, SD")),
    "watertown_sd": (44.909188, -97.155685, GAZ.format("4669300", "Watertown city, SD")),
    "janesville": (42.684698, -89.01317, GAZ.format("5537825", "Janesville city, WI")),
    "kewaskum": (43.517649, -88.230607, GAZ.format("5539300", "Kewaskum village, WI")),
    "madison": (43.087815, -89.429856, GAZ.format("5548000", "Madison city, WI")),
    "marshfield": (44.663291, -90.171736, GAZ.format("5549675", "Marshfield city, WI")),
    "stevens_point": (44.5253, -89.550377, GAZ.format("5577200", "Stevens Point city, WI")),
    "weyauwega": (44.323593, -88.933131, GAZ.format("5586400", "Weyauwega city, WI")),
    # L-091 Ontario + Québec
    "toronto": (43.7417, -79.3733, CGN.format("FEUZB") + ": Toronto"),
    "montreal": (45.508822, -73.554077, CGN.format("EHHUN") + ": Montréal"),
    "niagara_falls": (43.06012, -79.10671, CGN.format("FEDBA") + ": Niagara Falls"),
    "milton": (43.5055556, -79.9177778, CGN.format("FDGEM") + ": Milton (TC / Enbridge Gas Parkway station)"),
    "waddington": (44.861324, -75.197228, GAZ.format("3677728", "Waddington village, NY")),
    # L-093 pipeline lines (vertices). California: Census 2023 Gazetteer; eastern Canada: CER key points and CGNDB.
    "burney": (40.889362, -121.675465, GAZ.format("0609122", "Burney CDP, CA")),
    "red_bluff": (40.173515, -122.241525, GAZ.format("0659892", "Red Bluff city, CA")),
    "williams_ca": (39.14978, -122.138968, GAZ.format("0685586", "Williams city, CA")),
    "antioch": (37.978336, -121.796064, GAZ.format("0602252", "Antioch city, CA")),
    "barstow": (34.866987, -117.043035, GAZ.format("0604030", "Barstow city, CA")),
    "mojave": (35.014995, -118.190209, GAZ.format("0648452", "Mojave CDP, CA")),
    "kettleman_city": (36.008985, -119.962852, GAZ.format("0638394", "Kettleman City CDP, CA")),
    "milpitas": (37.433141, -121.890924, GAZ.format("0647766", "Milpitas city, CA")),
    "victorville": (34.527735, -117.353579, GAZ.format("0682590", "Victorville city, CA")),
    "adelanto": (34.580902, -117.439458, GAZ.format("0600296", "Adelanto city, CA")),
    "los_angeles": (34.019394, -118.410825, GAZ.format("0644000", "Los Angeles city, CA")),
    "desert_center": (33.737799, -115.366622, GAZ.format("0618982", "Desert Center CDP, CA")),
    "indio": (33.731639, -116.235971, GAZ.format("0636448", "Indio city, CA")),
    "moreno_valley": (33.923253, -117.205685, GAZ.format("0649270", "Moreno Valley city, CA")),
    "lebec": (34.844614, -118.899237, GAZ.format("0640956", "Lebec CDP, CA")),
    "santa_clarita": (34.416504, -118.500693, GAZ.format("0669088", "Santa Clarita city, CA")),
    "orillia": (44.6043, -79.42392, CGN.format("FDMLQ") + ": Orillia"),
    "barrie": (44.37124, -79.67697, CGN.format("FAFFD") + ": Barrie"),
    "kingston": (44.3066667, -76.4163888, CGN.format("FEVJR") + ": Kingston"),
    "ottawa": (45.33339, -75.58429, CGN.format("FEVNT") + ": Ottawa"),
    "cornwall": (45.0263889, -74.7263889, CGN.format("FCCOT") + ": Cornwall"),
    "trois_rivieres": (46.343077, -72.543383, CGN.format("EIITR") + ": Trois-Rivières"),
    "quebec_city": (46.813819, -71.207997, CGN.format("EHTWR") + ": Québec"),
    "parkway_kp": (43.5699, -79.7858, CER.format("Eastern Triangle - Parkway Deliveries", "TC Mainline")),
    "iroquois_kp": (44.8556, -75.2667, CER.format("Iroquois", "TC Mainline")),
    "niagara_kp": (43.1922, -79.0693, CER.format("Niagara", "TC Mainline")),
    "saint_lazare_kp": (45.3713, -74.1249, CER.format("Saint Lazare + Sabrevois", "TQM") + ": where TQM leaves the "
                        "Mainline, by Station 148 (Les Cèdres)"),
    "east_hereford_kp": (45.033, -71.5, CER.format("East Hereford", "TQM")),
    "other_us_ne_kp": (45.0236, -73.0546, CER.format("Other US Northeast", "TC Mainline") + ": Philipsburg / "
                       "Napierville"),
    "needles": (34.813426, -114.626639, GAZ.format("0650734", "Needles city, CA")),
    "blythe": (33.65352, -114.621823, GAZ.format("0607218", "Blythe city, CA")),
    "boron": (35.01978, -117.665938, GAZ.format("0607568", "Boron CDP, CA")),
    "mettler": (35.063563, -118.97298, GAZ.format("0647164", "Mettler CDP, CA")),
}
# Station 2: CER describes it as "just west of Chetwynd"; schematic offset from Chetwynd.
STN2 = [-121.75, 55.70]  # [lon, lat]


def pt(name):
    lat, lon, _ = P[name]
    return [round(lon, 5), round(lat, 5)]


def node(fid, name, typ, where, source, **props):
    lon, lat = where if isinstance(where, list) else pt(where)
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {"id": fid, "kind": "node", "system": "gas", "type": typ, "name": name,
                           "source": source if source else P[where][2], **props}}


def link(fid, name, op, pts, basis, **props):
    coords = [p if isinstance(p, list) else pt(p) for p in pts]
    def short(c):
        if "CGNDB" in c:
            gid = c.split("geonames/")[1].split()[0]
            return f"CGNDB {gid} ({c.rsplit(': ', 1)[-1].split(' (')[0]})"
        if c.startswith("US Census"):
            return "Census " + c.split("place ")[1].split(", internal")[0]
        return c.split(", https")[0].split(" (gis.dnr")[0]
    cites = [short(P[p][2]) for p in pts if isinstance(p, str)]
    return {"type": "Feature", "geometry": {"type": "LineString", "coordinates": coords},
            "properties": {"id": fid, "kind": "link", "system": "gas", "type": "pipeline", "name": name,
                           "operator": op, "schematic": True, "direction": 1,
                           "source": f"schematic: {basis}; vertices from " + "; ".join(cites)
                           + ". CGNDB = Canadian Geographical Names Database (NRCan, OGL-Canada); CER key points:"
                           " github.com/mbradds/pipeline-profiles key_points.csv (CER open data, OGL-Canada)"
                           + ("; Census = US Census Bureau 2023 Gazetteer place internal points (public domain)"
                              if any(isinstance(q, str) and P[q][2].startswith("US Census") for q in pts) else ""),
                           **props}}


# PLAN 4b share rows. of_band = the basis estimate's band (#208): total export carries CGL direct (±23 % of ~1.4 of ~11 Bcf/d)
EXPORT_SHARE = {"of": "export.total.flow_est", "label": "% of total export", "of_band": 0.03}
# L-070/L-071: the export points' share is of gas leaving the AB/NE BC production area by pipeline (Erik's "Exports" box;
# measured, so no of_band); LNG keeps total export
PIPE_SHARE = {"of": "export.pipeline.total_est", "label": "% of gas leaving the production area (pipelines)"}
BC_SHARE = {"of": "production.bc.daily_est", "label": "% of BC production", "of_band": 0.05}
# Part rows: estimated parts carry `band`, which renders "≈ … ±" (#211), so labels don't repeat it. Measured rows stay
# plain. From the item 4
# back-test (ngtl_split.py): province-share P90 AB 3.1 %, BC 7.7 %; "everything else" |error| P90 AB 401 ÷ 1,360 = 30 % (L-088: R_AB holds the NGTL basis),
# BC absolute ±300 MMcf/d in the label, no relative band (R_BC sits near 0, where "band" × value collapses; Reviewer on
# #298); the CGL-direct carry ±23 % (gas_lng_bcer). Alliance and
# T-North into NGTL are measured.
PROD_NOTE = ("today = a sum of public meters (NGTL field receipts, T-North into NGTL, Alliance, CGL direct) + everything "
             "else, carried from the last regulator month (Petrinex AB / BCER BC)")      # Erik via the orchestrator
W = "westcoast.oa."      # Westcoast scheduled volumes (gas_westcoast_oa, PLAN 6A)
ZONE1_LABEL = "Alliance Zone 1 (BC + NW AB)"
TOTAL_SHARE = {"of": "production.total.daily_est", "label": "% of AB + BC production", "of_band": 0.04}
DEMAND_SHARE = {"of": "ngtl.gdsr.intraprovincial.flow", "label": "% of AB demand"}                   # measured: no of_band


def panel(*series, events=None):
    d = {"series": list(series)}
    if events:
        d["events_ref"] = events
    return d


nodes = [
    node("gas.montney_receipts", "NGTL total field receipts (all AB + NE BC)", "receipt",
         [round((P["dawson_creek"][1] + P["grande_prairie"][1]) / 2, 5),
          round((P["dawson_creek"][0] + P["grande_prairie"][0]) / 2, 5)],
         "schematic: anchor in the Montney fairway midway between Dawson Creek (CGNDB JAPCN) and "
         "Grande Prairie (CGNDB IAHMM); the value is NGTL total field receipts, not Montney alone",
         value="ngtl.csr.field_receipts", unit="MMcf/d", always_show=True,
         panel=panel("ngtl.csr.field_receipts", "ngtl.csr.total_receipts", "ngtl.gdsr.field_receipts.flow",
                     "ngtl.csr.linepack", "ngtl.csr.linepack_target", events="gas.ngtl.usjr"),
         callout={"label": "NGTL total field receipts", "priority": 1}, region_value=True),
    node("gas.empress", "Empress (NGTL → TC Mainline)", "border", "empress", None,
         value="ngtl.csr.empress", unit="MMcf/d", share=PIPE_SHARE,
         panel=panel("ngtl.csr.empress", "ngtl.gdsr.empress.flow", "ngtl.gdsr.empress.nom",
                     events="gas.ngtl.egat"),
         callout={"label": "Empress", "priority": 1}),
    node("gas.mcneill", "McNeill (NGTL → Foothills SK)", "border", "mcneill", None,
         value="ngtl.csr.mcneill", unit="MMcf/d", share=PIPE_SHARE,
         panel=panel("ngtl.csr.mcneill", "ngtl.gdsr.mcneill.flow", "ngtl.gdsr.mcneill.nom",
                     events="gas.ngtl.egat"),
         callout={"label": "McNeill", "priority": 2}),
    node("gas.alberta_bc", "AB-BC border (NGTL → Foothills BC)", "border", "west_gate", None,
         value="ngtl.csr.alberta_bc", unit="MMcf/d", share=PIPE_SHARE,
         panel=panel("ngtl.csr.alberta_bc", "ngtl.gdsr.alberta_bc.flow", "ngtl.gdsr.alberta_bc.nom",
                     events="gas.ngtl.wgat"),
         callout={"label": "AB-BC border", "priority": 1}),
    node("gas.kingsgate", "Kingsgate (Foothills → GTN)", "border", "kingsgate", None,
         # L-085: GTN's scheduled receipts at Kingsgate (gas_gtn); the capacity is GTN's Flow Past Kingsgate
         value="gtn.kingsgate.receipts", capacity="gtn.kingsgate.capacity", unit="MMcf/d",
         panel=panel("gtn.kingsgate.receipts", "gtn.kingsgate.capacity", "ngtl.csr.alberta_bc",
                     "cer.foothills.kingsgate.throughput_q", events="gas.ngtl.fhbc"),
         callout={"label": "Kingsgate → GTN (scheduled)", "priority": 2}),
    node("gas.monchy", "Monchy / Port of Morgan (Foothills → Northern Border)", "border", "monchy", None,
         value="nbpl.port_of_morgan.receipts", unit="MMcf/d", flow="supply",       # L-099
         panel=panel("nbpl.port_of_morgan.receipts", "ngtl.csr.mcneill", events="gas.ngtl.fhsk"),
         callout={"label": "Port of Morgan: Canadian gas into Northern Border (scheduled)", "priority": 2}),
    node("gas.willow_valley", "Willow Valley Interconnect (NGTL → Coastal GasLink)", "interconnect",
         "wilde_lake", None, value="ngtl.csr.willow_valley", unit="MMcf/d",
         panel=panel("ngtl.csr.willow_valley", "ngtl.gdsr.willow_valley.flow", "ngtl.gdsr.willow_valley.nom",
                     events="gas.ngtl.usjr"),
         callout={"label": "NGTL → CGL (LNG Canada)", "priority": 2}),
    node("gas.gordondale", "Gordondale (receipt into NGTL)", "receipt", "gordondale", None,
         value="ngtl.csr.gordondale", unit="MMcf/d", share=BC_SHARE,
         panel=panel("ngtl.csr.gordondale", "ngtl.gdsr.gordondale.flow", events="gas.ngtl.usjr"),
         callout={"label": "Gordondale", "priority": 3}),
    node("gas.groundbirch_east", "Groundbirch East (receipt into NGTL)", "receipt", "groundbirch", None,
         value="ngtl.csr.groundbirch_east", unit="MMcf/d", share=BC_SHARE,
         panel=panel("ngtl.csr.groundbirch_east", "ngtl.gdsr.groundbirch_east.flow", events="gas.ngtl.usjr"),
         callout={"label": "Groundbirch East", "priority": 3}),
    node("gas.station2", "Westcoast Station 2", "hub", STN2,
         "schematic: CER describes Westcoast Station 2 as 'just west of Chetwynd' "
         "(Canada's Pipeline Transportation System 2016, Westcoast); placed ~7 km west of Chetwynd (CGNDB JCPML)",
         # PLAN 6A (Erik: scheduled volumes, not capacity): the value is Westcoast's scheduled flow south of Station 2
         # (gas_westcoast_oa); capacity stays as capacity.
         value="westcoast.oa.station2_south.scheduled", capacity="westcoast.oa.station2_south.capacity", unit="MMcf/d",
         panel=panel("westcoast.oa.station2_south.scheduled", "westcoast.oa.station2_south.capacity",
                     "westcoast.station2_south.capacity", events="gas.station2"),
         callout={"label": "Station 2 South (scheduled)", "priority": 1}),
    node("gas.huntingdon", "Huntingdon / Sumas (Westcoast → NWP)", "border", "huntingdon", None,
         value="westcoast.oa.huntingdon.scheduled", capacity="westcoast.oa.huntingdon.capacity", unit="MMcf/d",
         panel=panel("westcoast.oa.huntingdon.scheduled", "westcoast.oa.huntingdon.capacity",
                     "westcoast.huntingdon.capacity", "cer.westcoast.huntingdon_export.throughput_q",
                     "cer.westcoast.fortisbc_lm.throughput_q", "nwp.sumas.receipts",
                     "fortisbc.lower_mainland.demand_est", events="gas.huntingdon"),
         callout={"label": "Huntingdon (scheduled)", "priority": 2},
         # L-085: where Huntingdon's gas goes. The export is NWP's measured-basis Sumas receipt; the Lower Mainland is
         # the remainder less Westcoast's scheduling bias (pnw_gas.py: CER back-test, n 242, ±100 P90)
         callouts_extra=[
             {"depth": 1, "label": "export to NWP at Sumas", "series": "nwp.sumas.receipts", "unit": "MMcf/d",
              "priority": 2, "share": {"of": "westcoast.oa.huntingdon.scheduled", "label": "% of Huntingdon",
                                              # heat bases differ: Sumas 1,037 Btu/cf vs Westcoast GJ÷40 (~1,074),
                                              # so the share reads ~3-4 % high (Reviewer on #336)
                                              "of_band": 0.04}},
             {"depth": 1, "label": "Lower Mainland demand (derived, est. ±100)",
              "series": "fortisbc.lower_mainland.demand_est", "unit": "MMcf/d", "priority": 2}]),
    # PLAN 6A: Westcoast T-North as one box. Scheduled deliveries out of T-North = T-South + to NGTL at Sunset Creek +
    # to NGTL at Gordondale (26" Alberta East). That includes Aitken Creek storage, which Westcoast doesn't post apart.
    node("gas.westcoast.tnorth", "Westcoast T-North production (scheduled receipts less Aitken Creek storage)", "system",
         "fort_st_john", "schematic: T-North-wide value, anchored at " + P["fort_st_john"][2],
         # PLAN 6A (Erik: Aitken "seperated out"): production = T-North's measured scheduled deliveries out − Aitken Creek
         # storage (est.: today's lateral − the lateral's own production in the latest BCER month; gas_westcoast_oa).
         # band: ±164 MMcf/d within a storage season, wider (|ΔP|) across one; ~7-14 % (gas_westcoast_oa). Stated here
         # at the season-turn width.
         value="westcoast.oa.tnorth.production_est", unit="MMcf/d", band=0.14, region_value=True,
         note=("T-North production = Westcoast's scheduled deliveries out of T-North − Aitken Creek storage (est.; + = "
               "withdrawal, − = injection). Storage = today's Aitken Creek Lateral − the lateral's own production in the "
               "same month last year (BCER-measured storage net); band max(±164 MMcf/d, the gap to the latest BCER "
               "month); no same-month anchor → no estimate."),
         panel=panel("westcoast.oa.tnorth.production_est", "westcoast.oa.tnorth.out", "westcoast.oa.aitken_storage_est",
                     "bcer.aitken_storage.net_withdrawal", "westcoast.oa.station2_south.scheduled",
                     "westcoast.oa.sunset_creek.to_ngtl", "westcoast.oa.alberta_east.scheduled",
                     "westcoast.oa.fort_nelson.scheduled", "westcoast.oa.aitken_lateral.scheduled",
                     "westcoast.oa.fort_st_john.scheduled"),
         callout={"label": "T-North production (scheduled, est., ±7-14 %)", "priority": 1},
         callouts_extra=[{"label": "T-North receipts, measured (incl. Aitken storage)", "series": "westcoast.oa.tnorth.out",
                          "unit": "MMcf/d", "priority": 1},
                         {"label": "− Aitken storage (est.)",
                          "series": "westcoast.oa.aitken_storage_est", "unit": "MMcf/d", "priority": 1},
                         {"label": "  ± Aitken estimate's band (MMcf/d)", "series": "westcoast.oa.aitken_storage_band",
                          "unit": "MMcf/d", "priority": 2},
                         {"label": "· to T-South (Station 2 South)", "series": "westcoast.oa.station2_south.scheduled",
                          "unit": "MMcf/d", "priority": 2},
                         {"label": "· to NGTL at Sunset Creek", "series": "westcoast.oa.sunset_creek.to_ngtl",
                          "unit": "MMcf/d", "priority": 2},
                         {"label": "· to NGTL at Gordondale (26\" Alberta East)",
                          "series": "westcoast.oa.alberta_east.scheduled", "unit": "MMcf/d", "priority": 2}]),
    # Erik (L-066): LNG export = Willow Valley (NGTL, measured) + Sunset Creek (Westcoast scheduled, measured) + derived
    # D/C (est.: receipts − both, the gas_lng_bcer regression). Only the D/C is estimated; the box's band is its band
    # (±166 MMcf/d ≈ ±12 % of export today).
    node("gas.kitimat", "Kitimat (LNG Canada)", "lng", "kitimat", None,
         value="lng.export.total", unit="MMcf/d", band=0.12, share=EXPORT_SHARE,
         panel=panel("lng.export.total", "lng.export.willow_valley", "lng.export.sunset", "lng.export.dc",
                     "lng.kitimat.feed_30d", "lng.kitimat.cargo", "lng.lng_canada.receipts", "cgl.producer_direct",
                     "lng.lng_canada.fuel", "lng.lng_canada.flare", events="gas.kitimat"),
         callout={"label": "LNG export (daily)", "priority": 1}, always_show=True,
         callouts_extra=[{"label": "Willow Valley (NGTL → CGL)", "series": "lng.export.willow_valley", "unit": "MMcf/d",
                          "priority": 1},
                         {"label": "Sunset Creek (Westcoast, scheduled)", "series": "lng.export.sunset", "unit": "MMcf/d",
                          "priority": 1},
                         {"band": 0.28, "label": "D/C, producers direct into CGL (est.)", "series": "lng.export.dc",
                          "unit": "MMcf/d", "priority": 1},
                         {"band": 0.1, "label": "cross-check: cargoes, 30-day", "series": "lng.kitimat.feed_30d",
                          "unit": "MMcf/d", "priority": 2},
                         {"label": "BCER feed gas, last month", "series": "lng.lng_canada.receipts", "unit": "MMcf/d",
                          "priority": 2}]),
    node("gas.ngtl.linepack", "NGTL linepack (system)", "system", "usjr_kp",
         "schematic: system-wide value, anchored at the " + P["usjr_kp"][2],
         value="ngtl.csr.linepack", target="ngtl.csr.linepack_target", compare="1h", unit="MMcf",
         panel=panel("ngtl.csr.linepack", "ngtl.csr.linepack_target", "ngtl.csr.linepack_4h_roc",
                     "ngtl.gdsr.eod_linepack.flow", "ngtl.csr.flow_differential"),
         callout={"label": "NGTL linepack vs target", "priority": 1}),
    # PLAN 4b / L-058: intra-AB gas demand on a gas box (always shown). System-wide value: NGTL intraprovincial deliveries.
    node("gas.ab_demand", "Alberta gas demand (NGTL intra-AB deliveries)", "system",
         [round((P["calgary_kp"][1] + P["edmonton"][1]) / 2, 5), round((P["calgary_kp"][0] + P["edmonton"][0]) / 2, 5)],
         "schematic: system-wide value, anchored midway between the CER Calgary key point and Edmonton (CGNDB IACMP)",
         # PLAN step 5 item 2: the daily gas-day value (GDSR), so the population / industry rows add up to it; the live
         # CSR rate stays in the panel. Rows: ngtl_city.py (population HDD fit; industry = intra − population, and parts).
         value="ngtl.gdsr.intraprovincial.flow", unit="MMcf/d", region_value=True, always_show=True,
         panel=panel("ngtl.gdsr.intraprovincial.flow", "ngtl.csr.intraprovincial", "demand.ab.population_est",
                     "demand.ab.population_rest_est", "demand.ab.industry_est", "ngtl.dop.osda.flow", "ngtl.city.fort_sask.demand_est",
                     "demand.ab.power_burn_est", "demand.ab.other_industry_est", "demand.ab.population.monthly"),
         callout={"label": "AB gas demand (intra-AB)", "priority": 1},
         callouts_extra=[
             {"band": 0.24, "label": "Population (residential/commercial)", "series": "demand.ab.population_est",
              "unit": "MMcf/d", "priority": 1, "share": DEMAND_SHARE},
             # Erik (L-072): population by city, like Industry's parts; the five sum to the population row
             {"band": 0.2, "label": "· Calgary", "series": "ngtl.city.calgary.demand_est", "unit": "MMcf/d",
              "priority": 2, "share": DEMAND_SHARE},
             {"band": 0.2, "label": "· Edmonton", "series": "ngtl.city.edmonton.demand_est", "unit": "MMcf/d",
              "priority": 2, "share": DEMAND_SHARE},
             {"band": 0.15, "label": "· Red Deer", "series": "ngtl.city.red_deer.demand_est", "unit": "MMcf/d",
              "priority": 2, "share": DEMAND_SHARE},
             {"band": 0.15, "label": "· Lethbridge", "series": "ngtl.city.lethbridge.demand_est", "unit": "MMcf/d",
              "priority": 2, "share": DEMAND_SHARE},
             {"band": 0.2, "label": "· Rest of AB", "series": "demand.ab.population_rest_est", "unit": "MMcf/d",
              "priority": 2, "share": DEMAND_SHARE},
             {"band": 0.04, "label": "Industry", "series": "demand.ab.industry_est", "unit": "MMcf/d", "priority": 1,
              "share": DEMAND_SHARE},
             {"label": "· oil sands (OSDA)", "series": "ngtl.dop.osda.flow", "unit": "MMcf/d", "priority": 2,
              "share": DEMAND_SHARE},
             {"band": 0.18, "label": "· Heartland petrochem (Fort Sask)", "series": "ngtl.city.fort_sask.demand_est",
              "unit": "MMcf/d", "priority": 2, "share": DEMAND_SHARE},
             {"band": 0.23, "label": "· power generation (outside oil sands / Heartland cogen)",
              "series": "demand.ab.power_burn_est", "unit": "MMcf/d", "priority": 2, "share": DEMAND_SHARE},
             {"band": 0.24, "label": "· other industrial", "series": "demand.ab.other_industry_est", "unit": "MMcf/d",
              "priority": 2, "share": DEMAND_SHARE}]),
    node("gas.alliance.receipts", "Alliance receipts (Zone 1 + Zone 2)", "receipt", "alliance_zone2", None,
         value="alliance.receipts.total", capacity="alliance.capacity.total", unit="MMcf/d",
         panel=panel("alliance.receipts.total", "alliance.zone1.receipts", "alliance.zone2.receipts",
                     "alliance.capacity.total", "alliance.zone1.capacity", "alliance.zone2.capacity"),
         callout={"label": "Alliance receipts", "priority": 1},
         callouts_extra=[{"label": ZONE1_LABEL, "series": "alliance.zone1.receipts", "unit": "MMcf/d",
                          "priority": 2},
                         {"label": "Zone 2 (AB, south)", "series": "alliance.zone2.receipts", "unit": "MMcf/d",
                          "priority": 2}]),
    # PLAN 4b item 6 (L-058): city gas demand today (montney/connectors/ngtl_city.py). Utility cities: a + b × today's HDD,
    # fitted on ATCO Gas district deliveries in TC's delivery files. Fort Sask: Heartland industrial, last month carried.
    *[node(f"gas.city.{c}", f"{label} gas demand ({kind})", "system", [lon, lat],
           f"schematic: city centre (the Open-Meteo point used for the {label} temperature); the value is {basis}",
           value=f"ngtl.city.{c}.demand_est", unit="MMcf/d", band=band, region_value=True, share=DEMAND_SHARE,
           panel=panel(f"ngtl.city.{c}.demand_est", f"ngtl.city.{c}.monthly"),
           callout={"label": f"{label} gas demand (today est., ±{band * 100:.0f} %)", "priority": prio})
      for c, label, kind, lat, lon, band, prio, basis in [
          ("calgary", "Calgary", "utility", 51.05, -114.07, 0.20, 1, "ATCO Gas district 44 deliveries"),
          ("edmonton", "Edmonton", "utility", 53.55, -113.49, 0.20, 1, "ATCO Gas district 21 deliveries"),
          ("lethbridge", "Lethbridge", "utility", 49.69, -112.84, 0.15, 2, "ATCO Gas district 51 deliveries"),
          ("red_deer", "Red Deer", "utility", 52.27, -113.81, 0.15, 2, "ATCO Gas district 31 deliveries"),
          ("fort_sask", "Fort Saskatchewan", "industrial", 53.7111, -113.2178, 0.18, 2,
           "22 named Heartland industrial delivery stations")]],
    node("gas.ngtl.border.ab_mt", "Alberta-Montana border (NGTL 3868)", "border", "ab_mt_border", None,
         panel=panel("ngtl.gdsr.other_borders.flow"), context=True),
    node("gas.ngtl.border.cold_lake", "Cold Lake border (NGTL 1417)", "border", "cold_lake_border", None,
         panel=panel("ngtl.gdsr.other_borders.flow"), context=True),
    node("gas.ngtl.border.unity", "Unity border (NGTL 1250)", "border", "unity_border", None,
         panel=panel("ngtl.gdsr.other_borders.flow"), context=True),
    # PLAN 4b item 4: total production by province, today (always shown). NGTL by province today + the part no daily feed
    # measures, carried from the latest regulator month (Petrinex AB / BCER BC); ngtl_split.py has the method and back-test.
    # PLAN step 5 item 1: the zoomed-out total production box at the Montney area (AB + BC). Rows: by pipeline (NGTL,
    # Alliance) and by province (AB, BC); the two pairs overlap, so each row is a share of the total, not a sum.
    node("gas.production.total", "AB + BC gas production", "system", "grande_prairie",
         "schematic: AB + BC value, anchored at " + P["grande_prairie"][2] + " (the Montney area)",
         value="production.total.daily_est", unit="MMcf/d", band=0.04, region_value=True, always_show=True,
         note=PROD_NOTE,
         panel=panel("production.total.daily_est", "production.total.ngtl_est", "alliance.receipts.total",
                     "production.ab.daily_est", "production.bc.daily_est", "production.total.ngtl_field_est",
                     "production.total.ngtl_field_measured_est", "ngtl.gdsr.field_receipts.flow"),
         callout={"label": "AB + BC gas production (today est., ±4 %)", "priority": 1},
         # Erik (L-066): rows in this order: Alberta, BC, NGTL, Alliance, T-North. L-073: nested by `depth` (Frontend's
         # contract: depth 1 = a part of the box's value, depth n+1 = a part of the depth-n row above; flat pre-order).
         # Each level sums exactly to its parent (T-North's two parts: receipts − storage). The depth-1 rows overlap one
         # another (by province vs by pipeline), so they're shares of the total, not a sum.
         callouts_extra=[
             {"depth": 1, "band": 0.04, "label": "Alberta", "series": "production.ab.daily_est", "unit": "MMcf/d",
              "priority": 1, "share": TOTAL_SHARE},
             {"depth": 2, "band": 0.03, "label": "NGTL field receipts (AB)", "series": "ngtl.province.ab.receipt_est",
              "unit": "MMcf/d", "priority": 2},
             {"depth": 2, "label": "Alliance Zone 2", "series": "production.ab.alliance_est", "unit": "MMcf/d",
              "priority": 2},
             {"depth": 2, "band": 0.30, "label": "everything else (last regulator month)",
              "series": "production.ab.other_est", "unit": "MMcf/d", "priority": 2},
             {"depth": 1, "band": 0.05, "label": "BC", "series": "production.bc.daily_est", "unit": "MMcf/d",
              "priority": 1, "share": TOTAL_SHARE},
             {"depth": 2, "band": 0.08, "label": "NGTL field receipts (BC)", "series": "ngtl.province.bc.receipt_est",
              "unit": "MMcf/d", "priority": 2},
             {"depth": 2, "label": "T-North into NGTL (Gordondale + Groundbirch East)",
              "series": "montney.bc.via_tnorth_est", "unit": "MMcf/d", "priority": 2},
             {"depth": 2, "label": ZONE1_LABEL, "series": "production.bc.alliance_est", "unit": "MMcf/d",
              "priority": 2},
             {"depth": 2, "band": 0.11, "label": "CGL direct (est.)", "series": "production.bc.cgl_direct_est",
              "unit": "MMcf/d", "priority": 2},
             {"depth": 2, "band": 0.22, "label": "T-North to T-South (net of Aitken storage, est.)",
              "series": "production.bc.tnorth_south_est", "unit": "MMcf/d", "priority": 2},
             {"depth": 2, "label": "everything else (last BCER month, est. ±300)", "series": "production.bc.other_est",
              "unit": "MMcf/d", "priority": 2},
             # L-088 (Erik): NGTL is TC's published GDSR figures (field receipts + Gordondale + Groundbirch East), exact,
             # so no band; the measured basis (× k ≈ 1.03) is in the panel and the remainders hold the difference
             {"depth": 1, "label": "NGTL", "series": "production.total.ngtl_est", "unit": "MMcf/d",
              "priority": 1, "share": TOTAL_SHARE},
             {"depth": 2, "label": "NGTL field receipts (TC GDSR)", "series": "production.total.ngtl_field_est",
              "unit": "MMcf/d", "priority": 2},
             {"depth": 3, "band": 0.007, "label": "Upstream James River (USJR)", "series": "ngtl.area.usjr.receipt_est",
              "unit": "MMcf/d", "priority": 2},
             {"depth": 3, "band": 0.06, "label": "outside USJR", "series": "ngtl.area.rest.receipt_est",
              "unit": "MMcf/d", "priority": 2},
             {"depth": 2, "label": "Gordondale (from Westcoast)", "series": "ngtl.gdsr.gordondale.flow",
              "unit": "MMcf/d", "priority": 2},
             {"depth": 2, "label": "Groundbirch East (from Westcoast)", "series": "ngtl.gdsr.groundbirch_east.flow",
              "unit": "MMcf/d", "priority": 2},
             {"depth": 1, "label": "Alliance", "series": "alliance.receipts.total", "unit": "MMcf/d", "priority": 1,
              "share": TOTAL_SHARE},
             {"depth": 2, "label": ZONE1_LABEL, "series": "alliance.zone1.receipts", "unit": "MMcf/d", "priority": 2},
             {"depth": 2, "label": "Alliance Zone 2 (AB)", "series": "alliance.zone2.receipts", "unit": "MMcf/d",
              "priority": 2},
             {"depth": 1, "band": 0.14, "label": "T-North (net of Aitken storage)",
              "series": "westcoast.oa.tnorth.production_est", "unit": "MMcf/d", "priority": 1, "share": TOTAL_SHARE},
             {"depth": 2, "label": "receipts, measured (incl. Aitken storage)", "series": "westcoast.oa.tnorth.out",
              "unit": "MMcf/d", "priority": 2},
             {"depth": 2, "label": "− Aitken storage (est.)", "series": "westcoast.oa.aitken_storage_est",
              "unit": "MMcf/d", "priority": 2}]),
    node("gas.production.ab", "Alberta gas production (all pipelines)", "system", "fox_creek",
         "schematic: province-wide value, anchored at " + P["fox_creek"][2] + " (centre of AB's Montney/Duvernay fairway)",
         value="production.ab.daily_est", unit="MMcf/d", band=0.04, region_value=True, always_show=True, note=PROD_NOTE,
         panel=panel("production.ab.daily_est", "ngtl.province.ab.receipt_est", "production.ab.alliance_est",
                     "production.ab.other_est", "production.ab.monthly", "ngtl.province.ab.receipt"),
         callout={"label": "AB gas production (today est., ±4 %)", "priority": 1},
         callouts_extra=[{"band": 0.03, "label": "NGTL field receipts (today)",
                          "series": "ngtl.province.ab.receipt_est", "unit": "MMcf/d", "priority": 2},
                         {"label": "+ Alliance Zone 2", "series": "production.ab.alliance_est", "unit": "MMcf/d",
                          "priority": 2},
                         {"band": 0.30, "label": "+ everything else (last regulator month)",
                          "series": "production.ab.other_est", "unit": "MMcf/d", "priority": 2},
                         {"label": "Petrinex, last month", "series": "production.ab.monthly", "unit": "MMcf/d",
                          "priority": 3}]),
    node("gas.production.bc", "BC gas production (all pipelines)", "system", "fort_st_john",
         "schematic: province-wide value, anchored at " + P["fort_st_john"][2],
         value="production.bc.daily_est", unit="MMcf/d", band=0.05, region_value=True, always_show=True, note=PROD_NOTE,
         panel=panel("production.bc.daily_est", "ngtl.province.bc.receipt_est", "montney.bc.via_tnorth_est",
                     "production.bc.alliance_est", "production.bc.cgl_direct_est", "production.bc.tnorth_south_est",
                     "production.bc.other_est",
                     "production.bc.monthly", "ngtl.province.bc.receipt"),
         callout={"label": "BC gas production (today est., ±5 %)", "priority": 1},
         callouts_extra=[{"band": 0.08, "label": "NGTL field receipts (today)",
                          "series": "ngtl.province.bc.receipt_est", "unit": "MMcf/d", "priority": 2},
                         {"label": "+ T-North into NGTL (Gordondale + Groundbirch East)",
                          "series": "montney.bc.via_tnorth_est", "unit": "MMcf/d", "priority": 2},
                         {"label": "+ " + ZONE1_LABEL, "series": "production.bc.alliance_est", "unit": "MMcf/d",
                          "priority": 2},
                         {"band": 0.11, "label": "+ CGL direct (daily regression)",
                          "series": "production.bc.cgl_direct_est", "unit": "MMcf/d", "priority": 2},
                         {"band": 0.22, "label": "+ Westcoast T-North to T-South (net of Aitken storage, est.)",
                          "series": "production.bc.tnorth_south_est", "unit": "MMcf/d", "priority": 2},
                         {"label": "+ everything else (last BCER month, est. ±300)",
                          "series": "production.bc.other_est", "unit": "MMcf/d", "priority": 2},
                         {"label": "BCER, last month", "series": "production.bc.monthly", "unit": "MMcf/d",
                          "priority": 3}]),
    node("gas.ngtl.storage", "NGTL storage (net, + = withdrawal)", "storage", "suffield",
         P["suffield"][2] + "; represents all NGTL-connected storage (AECO-C, Carbon, Crossfield, ...)",
         value="ngtl.csr.net_storage", unit="MMcf/d",
         panel=panel("ngtl.csr.net_storage", "ngtl.gdsr.net_storage.flow", "ngtl.gdsr.storage_receipts.flow",
                     "ngtl.gdsr.storage_deliveries.flow"),
         callout={"label": "NGTL storage (net)", "priority": 2}, always_show=True),
    # L-077 (Erik): the TC Mainline east to North Bay Junction. One GCTS request per day range, summed into province areas
    # on ingest (tc_mainline); capability from TC's weekly maintenance bulletin (tc_mainline_capability). Values are
    # TC's scheduled volumes; the through-flows are by difference, before compressor fuel: est. ±6 % (the NOL
    # back-test against the bulletin capability, montney/connectors/tc_mainline.py).
    # Exports out of the system (Emerson, the EDA endpoint) carry flow "supply": PLAN 4c would otherwise read their
    # "deliveries" series as demand (negative). Local deliveries in the area boxes stay demand.
    node("gas.tc.prairies", "TC Mainline Prairies Line: receipts at Empress + Saskatchewan (scheduled)", "system",
         "swift_current", "schematic: Prairies Line value (TC's capability segment), anchored on the line at "
         + P["swift_current"][2], value="tcm.prairies.receipts", capacity="tcm.prairies.capability", unit="MMcf/d",
         panel=panel("tcm.prairies.receipts", "tcm.prairies.capability", "ngtl.csr.empress",
                     "cer.mainline.prairies.throughput_q", "cer.mainline.prairies.capacity"),
         callout={"label": "Prairies Line (scheduled)", "priority": 2},
         callouts_extra=[{"label": "capability (TC bulletin)", "series": "tcm.prairies.capability", "unit": "MMcf/d",
                          "priority": 2},
                         {"label": "Empress handover (NGTL, measured)", "series": "ngtl.csr.empress",
                          "unit": "MMcf/d", "priority": 2}]),
    node("gas.tc.area.sk", "TC Mainline in Saskatchewan (scheduled)", "system", "regina",
         "schematic: Saskatchewan-wide value, anchored at " + P["regina"][2],
         value="tcm.area.sk.outflow_est", unit="MMcf/d", band=0.06,
         panel=panel("tcm.area.sk.outflow_est", "tcm.area.sk.deliveries", "tcm.area.sk.receipts"),
         callout={"label": "Saskatchewan: flow east into MB (scheduled, est.)", "priority": 2},
         callouts_extra=[{"label": "deliveries (SaskEnergy, Centra)", "series": "tcm.area.sk.deliveries",
                          "unit": "MMcf/d", "priority": 2},
                         {"label": "receipts", "series": "tcm.area.sk.receipts", "unit": "MMcf/d", "priority": 2}]),
    # L-106 8c (wcan): the region's two eastern border exits, as dots (no box: the region box sums the exits, and
    # the Saskatchewan and Alliance boxes already headline these values)
    node("gas.wcan.exit.manitoba", "TC Mainline into Manitoba (wcan exit, SK/MB border)", "border", "welwyn", None,
         value="tcm.area.sk.outflow_est", unit="MMcf/d", band=0.06,
         panel=panel("tcm.area.sk.outflow_est", "tcm.area.sk.deliveries", "tcm.area.sk.receipts")),
    node("gas.wcan.exit.alliance", "Alliance at the US border (wcan exit, by Elmore SK)", "border", "alliance_border",
         None, value="alliance.receipts.total", unit="MMcf/d",
         panel=panel("alliance.receipts.total", "alliance.capacity.total")),
    node("gas.tc.area.mb", "TC Mainline in Manitoba (scheduled)", "system", "winnipeg",
         "schematic: Manitoba-wide value, anchored at " + P["winnipeg"][2],
         value="tcm.area.mb.outflow_est", capacity="tcm.nol.capability", unit="MMcf/d", band=0.06,
         panel=panel("tcm.area.mb.outflow_est", "tcm.nol.capability", "tcm.area.mb.deliveries",
                     "tcm.emerson.deliveries", "tcm.area.mb.receipts", "tcm.emerson.receipts",
                     "cer.mainline.nol.throughput_q", "cer.mainline.nol.capacity"),
         callout={"label": "Manitoba: on into the NOL (scheduled, est.)", "priority": 2},
         callouts_extra=[{"label": "NOL capability (TC bulletin)", "series": "tcm.nol.capability", "unit": "MMcf/d",
                          "priority": 2},
                         {"label": "deliveries (Centra)", "series": "tcm.area.mb.deliveries", "unit": "MMcf/d",
                          "priority": 2},
                         {"label": "to Emerson (US export)", "series": "tcm.emerson.deliveries", "unit": "MMcf/d",
                          "priority": 2, "flow": "supply"}]),
    node("gas.tc.emerson", "Emerson (TC Mainline → Viking / Great Lakes, US export)", "border", "emerson", None,
         value="tcm.emerson.deliveries", capacity="tcm.emerson.capability", unit="MMcf/d", flow="supply",
         panel=panel("tcm.emerson.deliveries", "tcm.emerson.capability", "tcm.emerson.receipts",
                     "tcm.emerson.glgt_deliveries", "tcm.emerson.viking_deliveries",
                     "cer.mainline.emerson.throughput_q", "cer.mainline.emerson.capacity"),
         callout={"label": "Emerson exports (scheduled)", "priority": 2},
         callouts_extra=[{"label": "Emerson Extension capability (TC bulletin)", "series": "tcm.emerson.capability",
                          "unit": "MMcf/d", "priority": 2},
                         {"label": "to Great Lakes (Emerson 2)", "series": "tcm.emerson.glgt_deliveries",
                          "unit": "MMcf/d", "priority": 2, "flow": "supply"},
                         {"label": "to Viking (Emerson 1)", "series": "tcm.emerson.viking_deliveries",
                          "unit": "MMcf/d", "priority": 2, "flow": "supply"}]),
    # L-106 8c (sont / mbnwo): where the Northern Ontario Line hands over to sont, as a dot (the NW Ontario box at
    # Thunder Bay already headlines the value)
    node("gas.sont.entry.nol", "Northern Ontario Line arriving at North Bay Junction (sont entry, from mbnwo)",
         "border", "nbj", None, value="tcm.area.on.outflow_est", unit="MMcf/d", band=0.06, flow="supply",
         panel=panel("tcm.area.on.outflow_est", "tcm.area.on.deliveries", "tcm.area.mb.outflow_est")),
    node("gas.tc.area.on", "TC Mainline in Ontario west of North Bay Junction (NOL; scheduled)", "system", "thunder_bay",
         "schematic: value for the NOL and the Sault, anchored at " + P["thunder_bay"][2],
         value="tcm.area.on.outflow_est", unit="MMcf/d", band=0.06,
         panel=panel("tcm.area.on.outflow_est", "tcm.area.on.deliveries", "tcm.area.on.receipts",
                     "tcm.area.mb.outflow_est"),
         callout={"label": "NW Ontario: arriving at North Bay Jct (scheduled, est.)", "priority": 2},
         callouts_extra=[{"label": "deliveries (NOL towns, Sault)", "series": "tcm.area.on.deliveries",
                          "unit": "MMcf/d", "priority": 2},
                         {"label": "receipts (Sault Ste. Marie)", "series": "tcm.area.on.receipts", "unit": "MMcf/d",
                          "priority": 2}]),
    node("gas.tc.eda", "TC Mainline Eastern Delivery Area, the endpoint (deliveries − receipts, scheduled)", "system",
         "nbj", "schematic: EDA-wide value (eastern Ontario and Québec, downstream), anchored at North Bay Junction: "
         + P["nbj"][2], value="tcm.eda.deliveries", capacity="tcm.eda.capability", unit="MMcf/d", flow="supply",
         panel=panel("tcm.eda.deliveries", "tcm.eda.capability", "tcm.stn148.capability"),
         callout={"label": "EDA, the endpoint (scheduled)", "priority": 2},
         callouts_extra=[{"label": "EDA capability (TC bulletin)", "series": "tcm.eda.capability", "unit": "MMcf/d",
                          "priority": 2}]),
    # L-080 item 1 (Erik): boxes for the fully zoomed-out view, from the same TC Mainline series (no new data). Frontend
    # sets their tier-0 placement. Signs (PLAN 4c): "deliveries" read as demand (−); the exports carry flow "supply"
    # (#293). Through-flows by difference carry the NOL back-test band (±6 %, tc_mainline.py).
    node("gas.tc.overview.sk_demand", "Saskatchewan demand on the TC Mainline (scheduled deliveries)", "system",
         "moose_jaw", "schematic: Saskatchewan-wide value, anchored at " + P["moose_jaw"][2],
         value="tcm.area.sk.deliveries", unit="MMcf/d",
         note=("Only TC Mainline's Saskatchewan deliveries (SaskEnergy/TransGas, Centra's SK delivery area), not all of "
               "Saskatchewan's demand. SaskEnergy also takes Saskatchewan's own production and gas off Foothills."),
         panel=panel("tcm.area.sk.deliveries", "tcm.area.sk.receipts", "tcm.area.sk.outflow_est"),
         callout={"label": "SK demand (TC Mainline deliveries, scheduled)", "priority": 1}),
    node("gas.tc.overview.mb_demand", "Manitoba demand on the TC Mainline (scheduled deliveries)", "system",
         "brandon", "schematic: Manitoba-wide value, anchored at " + P["brandon"][2],
         value="tcm.area.mb.deliveries", unit="MMcf/d",
         note=("TC Mainline's Manitoba deliveries (Centra's delivery areas), Emerson exports excluded. Manitoba has no gas "
               "production, so this is close to all of Manitoba's gas demand."),
         panel=panel("tcm.area.mb.deliveries", "tcm.area.mb.receipts", "tcm.emerson.deliveries"),
         callout={"label": "MB demand (TC Mainline deliveries, scheduled)", "priority": 1}),
    node("gas.tc.overview.mainline", "TC Mainline east of Empress: Prairies Line and Emerson lateral",
         "system", "mainline_nol", "schematic: Mainline-wide value, anchored where the Prairies Line splits into Emerson "
         "and the NOL: " + P["mainline_nol"][2],
         value="tcm.prairies.receipts", unit="MMcf/d",
         share={"of": "tcm.prairies.capability", "label": "% of Prairies Line capability"},
         panel=panel("tcm.prairies.receipts", "tcm.prairies.capability", "tcm.emerson.deliveries",
                     "tcm.emerson.capability"),
         callout={"label": "Mainline east of Empress: Prairies Line (scheduled)", "priority": 1},
         # L-082 (Erik): the NOL headlines the Ontario box now, so it isn't a row here too
         callouts_extra=[
             {"depth": 1, "label": "Emerson lateral (US export)", "series": "tcm.emerson.deliveries", "unit": "MMcf/d",
              "priority": 1, "flow": "supply",
              "share": {"of": "tcm.prairies.receipts", "label": "% of the Prairies Line"}}]),
    # L-082 (Erik): "instead of EDA endpoint, can you show NOL and western Ontario demand as the same box". EDA stays on
    # the zoomed-in gas.tc.eda box.
    node("gas.tc.overview.ontario", "Ontario on the TC Mainline: the Northern Ontario Line and western Ontario demand",
         "system", "kapuskasing", "schematic: Ontario-wide value, anchored on the NOL at " + P["kapuskasing"][2],
         value="tcm.area.mb.outflow_est", unit="MMcf/d", band=0.06,
         share={"of": "tcm.nol.capability", "label": "% of NOL capability"},     # TC-published: no of_band
         note=("NOL = the Northern Ontario Line's flow east of Station 41 into Ontario (TC scheduled volumes by difference, "
               "before compressor fuel; est. ±6 %, the #289 back-test). Western Ontario demand = TC's deliveries on the "
               "NOL and at the Sault (west of North Bay Junction). Southern and eastern Ontario aren't in either."),
         panel=panel("tcm.area.mb.outflow_est", "tcm.nol.capability", "tcm.area.on.deliveries", "tcm.area.on.outflow_est"),
         callout={"label": "Ontario: Northern Ontario Line (scheduled, est.)", "priority": 1},
         callouts_extra=[
             {"depth": 1, "label": "western Ontario demand (NOL towns, Sault)", "series": "tcm.area.on.deliveries",
              "unit": "MMcf/d", "priority": 1,
              "share": {"of": "tcm.area.mb.outflow_est", "label": "% of the NOL", "of_band": 0.06}}]),
]

# L-085 (Erik: "fill in PacNW & BC demand … the derived Vancouver demand, Huntingdon exports to NWPL, JP storage,
# Stanfield, Malin"). NWP and GTN scheduled volumes (gas_nwp, gas_gtn: montney/connectors/pnw_gas.py), MMBtu at
# 1,037 Btu/cf. Signs (PLAN 4c): demand reads negative by its id; GTN's exports (Malin, Tuscarora) are "deliveries"
# by name, so they carry flow "supply" like Emerson (#293); storage net is + withdrawal.
nodes += [
    # L-089 (Erik: "for BC include the implied gas demand"): BC demand off Westcoast T-South (pnw_gas.py 3b, ±110 vs
    # StatCan's BC distribution deliveries). Its own feature so the production box's "+" parts still sum.
    node("gas.bc.demand", "BC gas demand off Westcoast T-South, derived", "system", "kamloops", None,
         value="demand.bc.implied_est", unit="MMcf/d",
         note=("Westcoast Station 2 South (scheduled) less the export at Huntingdon/Sumas: what T-South delivers inside "
               "BC (FortisBC Lower Mainland, Vancouver Island and Interior off Westcoast, Pacific Northern Gas, T-South "
               "fuel). Not in it: LNG Canada's plant fuel, NE BC industry and field use, the Kootenays (Alberta gas via "
               "Southern Crossing). Back-test against StatCan's monthly BC distribution deliveries (Nov 2025-Aug 2026): "
               "±110 MMcf/d (P90)."),
         panel=panel("demand.bc.implied_est", "westcoast.oa.station2_south.scheduled", "nwp.sumas.receipts",
                     "fortisbc.lower_mainland.demand_est"),
         callout={"label": "BC demand (off T-South, est. ±110)", "priority": 1}),
    node("gas.bc.lower_mainland", "Lower Mainland (Vancouver) demand, derived", "system", "vancouver", None,
         value="fortisbc.lower_mainland.demand_est", unit="MMcf/d",
         note=("Westcoast's Huntingdon Delivery Area (scheduled) less NWP's Sumas receipts (the export) less Westcoast's "
               "scheduling bias for the month. Back-test against CER's measured key points (Nov 2025-Jun 2026, n 242): "
               "±100 MMcf/d (P90). FortisBC Lower Mainland, incl. Vancouver Island's supply via the Lower Mainland."),
         panel=panel("fortisbc.lower_mainland.demand_est", "westcoast.oa.huntingdon.scheduled", "nwp.sumas.receipts",
                     "cer.westcoast.fortisbc_lm.throughput_q", "cer.westcoast.fortisbc_lm.capacity"),
         callout={"label": "Lower Mainland demand (derived, est. ±100)", "priority": 1}),
    node("gas.nwp.sumas", "Sumas (NWP receipt from Westcoast)", "border", "sumas", None,
         value="nwp.sumas.receipts", capacity="nwp.sumas.capacity", unit="MMcf/d",
         panel=panel("nwp.sumas.receipts", "nwp.sumas.capacity", "westcoast.oa.huntingdon.scheduled",
                     "cer.westcoast.huntingdon_export.throughput_q"),
         callout={"label": "Sumas → NWP (BC export, scheduled)", "priority": 2}),
    node("gas.nwp.jackson_prairie", "Jackson Prairie storage (net, + = withdrawal)", "storage", "jackson_prairie", None,
         value="nwp.jackson_prairie.net_withdrawal", unit="MMcf/d",
         note=("Net of NWP's scheduled receipts from (withdrawal) and deliveries into (injection) Jackson Prairie, Puget "
               "Sound Energy's aquifer storage near Chehalis. Inventory: NWP's preliminary storage levels for the whole "
               "facility (PSE, NWP and Avista shares)."),
         panel=panel("nwp.jackson_prairie.net_withdrawal", "nwp.jackson_prairie.inventory",
                     "nwp.jackson_prairie.capacity"),
         callout={"label": "Jackson Prairie storage (net)", "priority": 2},
         callouts_extra=[{"depth": 1, "label": "inventory", "series": "nwp.jackson_prairie.inventory", "unit": "Bcf",
                          "priority": 2, "share": {"of": "nwp.jackson_prairie.capacity", "label": "% full"}}]),
    # L-106 8c (pnw): the region's Rockies entry, as a dot (the PacNW imports box already headlines it as a row)
    node("gas.pnw.entry.rockies", "NWP from the Rockies at Baker (pnw entry, net north)", "border", "baker_city", None,
         value="nwp.baker.net_north", unit="MMcf/d", flow="supply",
         panel=panel("nwp.baker.net_north", "pnw.imports.total")),
    node("gas.stanfield", "Stanfield (GTN → NWP)", "interconnect", "stanfield", None,
         value="gtn.stanfield.to_nwp", unit="MMcf/d",
         panel=panel("gtn.stanfield.to_nwp", "nwp.stanfield.from_gtn", "gtn.local.deliveries"),
         callout={"label": "Stanfield: GTN → NWP (scheduled)", "priority": 2}),
    node("gas.malin", "Malin (GTN → California)", "border", "malin", None,
         value="gtn.malin.deliveries", capacity="gtn.malin.capacity", unit="MMcf/d", flow="supply",
         panel=panel("gtn.malin.deliveries", "gtn.malin.capacity", "gtn.tuscarora.deliveries", "gtn.kingsgate.receipts"),
         callout={"label": "Malin → California (scheduled)", "priority": 2},
         callouts_extra=[{"depth": 1, "label": "to Tuscarora (Nevada)", "series": "gtn.tuscarora.deliveries",
                          "unit": "MMcf/d", "priority": 3, "flow": "supply"},
                         {"depth": 1, "label": "GTN local deliveries (ID, WA, OR)", "series": "gtn.local.deliveries",
                          "unit": "MMcf/d", "priority": 3},
                         # depth 0: a headline of its own, not a part of GTN's Malin value (Ruby is another pipeline;
                         # #351/#354). Last, because rows are pre-order: anything after it would nest under it (#355)
                         {"depth": 0, "label": "PG&E receipts at Malin (GTN + Ruby: California's northern imports)",
                          "series": "pge.imports.north", "unit": "MMcf/d", "priority": 2}]),
]

# L-086 (Erik: "make a PacNW Imports, which is King + Huntington + some NWPL number … from the Rockies … total gas
# demand for PacNW … total storage … visible when fully zoomed out"). Three features, one per headline, so each keeps
# its total when folded: Frontend merges them into one box (map.json merge_into + section heads, tier 0), like the
# Alberta demand box. Method, Baker as the Rockies entry, and the EIA back-test: montney/connectors/pnw_gas.py.
PNW_NOTE = ("PacNW = Washington + Oregon + north Idaho. Imports = GTN at Kingsgate + NWP at Sumas + NWP's net flow north "
            "at Baker, OR (the Rockies and southern Idaho; negative when PacNW gas flows east) + GTN's other receipts "
            "(Turquoise Flats, Rockies gas off Ruby; Ridge Road; RNG). Demand = imports − exits "
            "(Malin, Tuscarora) + storage net withdrawal (Jackson Prairie, Plymouth LNG); ±10 %, back-tested against "
            "EIA's WA + OR deliveries. NW Natural's Mist posts nothing daily, so its withdrawals aren't in the demand.")
nodes += [
    node("gas.pnw.imports", "PacNW gas imports (GTN Kingsgate + NWP Sumas + NWP from the Rockies + GTN's other receipts)",
         "system", "yakima",
         "schematic: PacNW-wide value, anchored at " + P["yakima"][2], value="pnw.imports.total", unit="MMcf/d",
         note=PNW_NOTE, region_value=True, always_show=True,
         panel=panel("pnw.imports.total", "gtn.kingsgate.receipts", "nwp.sumas.receipts", "nwp.baker.net_north",
                     "gtn.receipts.ruby_turquoise", "gtn.receipts.other"),
         callout={"label": "PacNW imports (scheduled)", "priority": 1},
         callouts_extra=[
             {"depth": 1, "label": "Kingsgate (GTN, from Alberta)", "series": "gtn.kingsgate.receipts",
              "unit": "MMcf/d", "priority": 1},
             {"depth": 1, "label": "Huntingdon / Sumas (NWP, from BC)", "series": "nwp.sumas.receipts",
              "unit": "MMcf/d", "priority": 1},
             {"depth": 1, "label": "Rockies via NWP at Baker, OR (net; − = east)", "series": "nwp.baker.net_north",
              "unit": "MMcf/d", "priority": 1},
             {"depth": 1, "label": "Turquoise Flats (GTN, Rockies gas off Ruby)",
              "series": "gtn.receipts.ruby_turquoise", "unit": "MMcf/d", "priority": 1},
             {"depth": 1, "label": "other GTN receipts (Ridge Road, RNG)", "series": "gtn.receipts.other",
              "unit": "MMcf/d", "priority": 1}]),
    node("gas.pnw.demand", "PacNW gas demand (WA + OR + north Idaho), derived", "system", "yakima",
         "schematic: PacNW-wide value, anchored at " + P["yakima"][2], value="pnw.demand_est", unit="MMcf/d",
         band=0.10, note=PNW_NOTE, region_value=True, always_show=True,
         panel=panel("pnw.demand_est", "pnw.imports.total", "pnw.exports.total", "pnw.storage.net_withdrawal",
                     "gtn.local.deliveries", "pnw.demand.nwp_est"),
         callout={"label": "PacNW demand (est., ±10 %)", "priority": 1},
         callouts_extra=[
             {"depth": 1, "label": "GTN local deliveries (ID, eastern WA, central OR)", "series": "gtn.local.deliveries",
              "unit": "MMcf/d", "priority": 1},
             {"depth": 1, "band": 0.10, "label": "off NWP (I-5, Columbia, by difference)", "series": "pnw.demand.nwp_est",
              "unit": "MMcf/d", "priority": 1}]),
    node("gas.pnw.storage", "PacNW gas storage (Jackson Prairie + Mist)", "storage", "yakima",
         "schematic: PacNW-wide value, anchored at " + P["yakima"][2], value="pnw.storage.inventory_est", unit="Bcf",
         note=("Jackson Prairie from NWP's daily preliminary storage levels; Mist (NW Natural) from EIA's monthly Oregon "
               "working gas, the last reported month (~2 months behind). Plymouth LNG's inventory isn't public; its "
               "daily flow is in the net."),
         region_value=True, always_show=True,
         panel=panel("pnw.storage.inventory_est", "nwp.jackson_prairie.inventory", "eia.or.storage.working_gas",
                     "pnw.storage.net_withdrawal", "nwp.jackson_prairie.net_withdrawal",
                     "nwp.plymouth_lng.net_withdrawal"),
         callout={"label": "PacNW storage (JP today + Mist, EIA's latest month)", "priority": 1},
         callouts_extra=[
             {"depth": 1, "label": "Jackson Prairie (today)", "series": "nwp.jackson_prairie.inventory", "unit": "Bcf",
              "priority": 1, "share": {"of": "nwp.jackson_prairie.capacity", "label": "% full"}},
             {"depth": 1, "label": "Mist (EIA, latest reported month, ~2-3 mo behind)",
              "series": "eia.or.storage.working_gas", "unit": "Bcf",
              "priority": 1}]),
]

# L-090 (Erik: "add California … total Cali nat gas demand, … storage, imports. Then break it out by PG&E and SoCal,
# Northern and southern imports"). PG&E Pipe Ranger + SoCalGas ENVOY (gas_pge, gas_socalgas; cal_gas.py). Three summary
# features, one per headline, for Frontend to merge into one tier-0 box (like PacNW); import-point nodes below.
# Signs (PLAN 4c): demand ids read negative; imports positive; storage net + withdrawal.
CAL_NOTE = ("PG&E + SoCalGas (incl. SDG&E): the two systems' own published demand. Not in it: Southwest Gas's California "
            "areas, Kern River/Mojave direct-connect customers (Kern County oil fields) and the independent storage "
            "fields' fuel, so it reads below EIA's California total (~5-6 Bcf/d). Northern imports = PG&E's receipts at "
            "Malin (GTN + Ruby); southern = PG&E's Baja path + SoCalGas's interstate points.")
nodes += [
    node("gas.cal.imports", "PG&E + SoCalGas gas imports: northern (Malin) and southern (Topock, Ehrenberg, Kern)",
         "system", "fresno", "schematic: California-wide value, anchored at " + P["fresno"][2],
         value="cal.imports.total", unit="MMcf/d", note=CAL_NOTE, region_value=True, always_show=True,
         panel=panel("cal.imports.total", "cal.imports.north", "cal.imports.south", "pge.imports.north",
                     "pge.imports.south", "socal.imports.south"),
         callout={"label": "PG&E + SoCalGas imports (scheduled)", "priority": 1},
         callouts_extra=[
             {"depth": 1, "label": "northern (Malin, into PG&E)", "series": "cal.imports.north", "unit": "MMcf/d",
              "priority": 1},
             {"depth": 2, "label": "GTN", "series": "pge.imports.gtn", "unit": "MMcf/d", "priority": 2},
             {"depth": 2, "label": "Ruby", "series": "pge.imports.ruby", "unit": "MMcf/d", "priority": 2},
             {"depth": 1, "label": "southern (PG&E Baja path + SoCalGas)", "series": "cal.imports.south",
              "unit": "MMcf/d", "priority": 1},
             {"depth": 2, "label": "into PG&E", "series": "pge.imports.south", "unit": "MMcf/d", "priority": 2},
             {"depth": 2, "label": "into SoCalGas", "series": "socal.imports.south", "unit": "MMcf/d", "priority": 2}]),
    node("gas.cal.demand", "PG&E + SoCalGas gas demand (incl. SDG&E)", "system", "fresno",
         "schematic: California-wide value, anchored at " + P["fresno"][2], value="cal.demand.total", unit="MMcf/d",
         note=CAL_NOTE, region_value=True, always_show=True,
         panel=panel("cal.demand.total", "pge.demand.total", "socal.demand.sendout", "pge.receipts.from_socal"),
         callout={"label": "PG&E + SoCalGas demand", "priority": 1},
         callouts_extra=[
             {"depth": 1, "label": "PG&E system demand", "series": "pge.demand.total", "unit": "MMcf/d", "priority": 1},
             {"depth": 2, "label": "core (residential, commercial)", "series": "pge.demand.core", "unit": "MMcf/d",
              "priority": 2},
             {"depth": 2, "label": "electric generation", "series": "pge.demand.electric_gen", "unit": "MMcf/d",
              "priority": 2},
             {"depth": 2, "label": "industrial", "series": "pge.demand.industrial", "unit": "MMcf/d", "priority": 2},
             {"depth": 1, "label": "SoCalGas sendout (incl. SDG&E)", "series": "socal.demand.sendout",
              "unit": "MMcf/d", "priority": 1},
             # Reviewer on #348: PG&E's receipts from SoCalGas are in both; stored positive, so this row reads + and
             # the three rows sum to the (negative) headline
             {"depth": 1, "label": "less PG&E's receipts from SoCalGas (counted in both)",
              "series": "pge.receipts.from_socal", "unit": "MMcf/d", "priority": 2}]),
    node("gas.cal.storage", "PG&E + SoCalGas storage inventory", "storage", "fresno",
         "schematic: California-wide value, anchored at " + P["fresno"][2], value="cal.storage.inventory", unit="Bcf",
         note=("PG&E's own working gas (posted as the next day's opening, so a day behind) + SoCalGas's ending balance "
               "(Aliso Canyon, Honor Rancho, La Goleta, Playa del Rey), on the latest day both have. Wild Goose, Lodi, "
               "Gill Ranch and Central Valley post daily net flows only; they're in the net, not the inventory."),
         region_value=True, always_show=True,
         panel=panel("cal.storage.inventory", "pge.storage.working_gas", "socal.storage.inventory",
                     "cal.storage.net_withdrawal", "pge.storage.net_withdrawal",
                     "pge.storage.independent_net_withdrawal", "socal.storage.net_withdrawal"),
         callout={"label": "PG&E + SoCalGas storage (inventory)", "priority": 1},
         callouts_extra=[
             {"depth": 1, "label": "PG&E working gas", "series": "pge.storage.working_gas", "unit": "Bcf",
              "priority": 1},
             {"depth": 1, "label": "SoCalGas", "series": "socal.storage.inventory", "unit": "Bcf", "priority": 1}]),
    # L-094: the Opal hub (the Rockies' gathering point for Kern River, Ruby and NWP), value-less
    node("gas.rockies.opal", "Opal hub (Rockies: Kern River, Ruby, Northwest Pipeline)", "hub", "opal", None,
         panel=panel("cal.imports.kern_mojave", "pge.imports.ruby", "nwp.baker.net_north")),
    # import points (southern imports by point sum to cal.imports.south)
    node("gas.cal.topock", "Topock / Needles (El Paso, Transwestern, Southern Trails into California)", "border",
         "needles", None, value="cal.imports.topock", unit="MMcf/d",
         panel=panel("cal.imports.topock", "pge.imports.el_paso", "pge.imports.transwestern",
                     "pge.imports.southern_trails", "socal.imports.north_needles", "socal.imports.topock_el_paso",
                     "socal.imports.topock_transwestern"),
         callout={"label": "Topock / Needles (scheduled)", "priority": 2}),
    node("gas.cal.ehrenberg", "Ehrenberg / Blythe (El Paso, North Baja into SoCalGas)", "border", "blythe", None,
         value="cal.imports.ehrenberg", unit="MMcf/d",
         panel=panel("cal.imports.ehrenberg", "socal.imports.ehrenberg", "socal.imports.blythe"),
         callout={"label": "Ehrenberg / Blythe (scheduled)", "priority": 2}),
    node("gas.cal.kramer", "Kramer Junction / Daggett (Kern River, Mojave into California)", "border", "boron",
         P["boron"][2] + "; Kramer Junction is ~10 km west of Boron, Daggett ~60 km east (schematic)",
         value="cal.imports.kramer", unit="MMcf/d",
         panel=panel("cal.imports.kramer", "socal.imports.kramer_junction", "pge.imports.kern_river"),
         callout={"label": "Kramer Junction (scheduled)", "priority": 2}),
    node("gas.cal.wheeler_ridge", "Wheeler Ridge (Kern River, Mojave into SoCalGas)", "border", "mettler",
         P["mettler"][2] + "; the Wheeler Ridge interconnect is just south of Mettler (schematic)",
         value="cal.imports.wheeler_ridge", unit="MMcf/d",
         panel=panel("cal.imports.wheeler_ridge", "socal.imports.wheeler_ridge", "socal.receipts.from_pge"),
         callout={"label": "Wheeler Ridge (scheduled)", "priority": 2}),
]

# L-091 (Erik: "keep the requests as limited as possible … keep it quite broad"). Ontario + Québec: TC Mainline points
# east of North Bay Junction from tc_mainline's existing request (tcm.*), and StatCan's monthly provincial supply and
# disposition (gas_statcan_onqc, statcan.*). Ontario's daily demand isn't public (Enbridge Gas's Gas Day Summary sits
# behind a reCAPTCHA), so its box headlines StatCan's latest month; Québec's is daily: Énergir off TC (±13 %).
nodes += [
    # L-093 part 2 (Erik: "Ontario demand should be an aggregate from mainline and dawn locations"; orchestrator: headline
    # it as supply). The id stays gas.ont.demand (map.json refers to it); the value is gas in, not demand.
    node("gas.ont.demand", "Ontario gas in: TC Mainline + Dawn inflows (daily; includes storage injection)", "system",
         "toronto", "schematic: Ontario-wide value, anchored at " + P["toronto"][2], value="supply.on.pipelines_est",
         # ±: the NOL's ±6 % (#289) on ~2.1 of ~2.8 Bcf/d (Reviewer on #379); the storage gap isn't a band (note)
         unit="MMcf/d", band=0.045, region_value=True, always_show=True,
         note=("Gas arriving in Ontario: the TC Mainline (NOL + Sault + Niagara, less Québec and Iroquois) plus the US "
               "pipelines landing at Dawn (Great Lakes and Vector at St. Clair, Panhandle at Ojibway). It isn't demand: "
               "Dawn/Tecumseh storage isn't public daily, so the gap to demand is storage, which StatCan puts at "
               "−2.7 Bcf/d (January withdrawals) to +1.7 Bcf/d (May injections); this reads high in summer and low in "
               "winter. Demand is StatCan's monthly figure, ~2 months behind."),
         panel=panel("supply.on.pipelines_est", "supply.on.mainline_est", "supply.on.dawn_inflows_est",
                     "statcan.on.deliveries", "statcan.on.storage_net_withdrawal", "statcan.on.storage"),
         callout={"label": "Ontario gas in: TC + Dawn inflows (daily; includes storage injection)", "priority": 1},
         callouts_extra=[{"depth": 1, "label": "Ontario demand (StatCan, monthly)", "series": "statcan.on.deliveries",
                          "unit": "MMcf/d", "priority": 1},
                         {"depth": 1, "label": "storage net withdrawal (StatCan, monthly; the gap to demand)",
                          "series": "statcan.on.storage_net_withdrawal", "unit": "MMcf/d", "priority": 1},
                         {"depth": 1, "band": 0.07, "label": "TC Mainline into Ontario (net of Québec, Iroquois)",
                          "series": "supply.on.mainline_est", "unit": "MMcf/d", "priority": 3},
                         # L-099: Great Lakes / Vector / Panhandle rows live in the Dawn box (gas.ont.dawn)
                         {"depth": 1, "label": "storage inventory (Dawn, Tecumseh; StatCan month end)",
                          "series": "statcan.on.storage", "unit": "Bcf", "priority": 3}]),
    node("gas.qc.demand", "Québec gas demand: Énergir off the TC Mainline (est.)", "system", "montreal",
         "schematic: Québec-wide value, anchored at " + P["montreal"][2], value="demand.qc.implied_est",
         unit="MMcf/d", band=0.13, region_value=True, always_show=True,
         note=("TC's scheduled deliveries to Énergir (EDA + NDA), which is nearly all of Québec's demand. Against StatCan's "
               "monthly Québec deliveries (Nov 2025-Aug 2026): −6 % on average, −10..−12 % Dec-Mar, when Québec's own "
               "storage (Pointe-du-Lac, Saint-Flavien) and Énergir's LNG plant cover the peaks. ±13 %."),
         panel=panel("demand.qc.implied_est", "statcan.qc.deliveries", "tcm.qc.exports", "tcm.stn148.capability",
                     "statcan.qc.storage", "tcm.eda.deliveries"),
         callout={"label": "Québec demand (Énergir off TC, est. ±13 %)", "priority": 1},
         callouts_extra=[{"depth": 1, "label": "StatCan deliveries, latest month", "series": "statcan.qc.deliveries",
                          "unit": "MMcf/d", "priority": 2},
                         {"depth": 1, "label": "exports from Québec (East Hereford, Philipsburg, Napierville)",
                          "series": "tcm.qc.exports", "unit": "MMcf/d", "priority": 2, "flow": "supply"},
                         {"depth": 1, "label": "TC capability downstream of Sta 148 (weekly bulletin)",
                          "series": "tcm.stn148.capability", "unit": "MMcf/d", "priority": 3},
                         {"depth": 1, "label": "storage inventory (StatCan month end)", "series": "statcan.qc.storage",
                          "unit": "Bcf", "priority": 3}]),
    node("gas.ont.parkway", "Parkway (TC Mainline ↔ Enbridge Gas Dawn-Parkway)", "interconnect", "milton", None,
         value="tcm.parkway.net_deliveries", unit="MMcf/d", flow="supply",     # a hand-off, not demand (PLAN 4c)
         panel=panel("tcm.parkway.net_deliveries", "tcm.kirkwall.net_deliveries", "tcm.cda.deliveries"),
         callout={"label": "Parkway: TC → Enbridge Gas (net, scheduled)", "priority": 2},
         callouts_extra=[{"depth": 1, "label": "TC to the GTA (CDA, scheduled)", "series": "tcm.cda.deliveries",
                          "unit": "MMcf/d", "priority": 2},
                         {"depth": 1, "label": "Kirkwall: TC → Enbridge Gas (net)",
                          "series": "tcm.kirkwall.net_deliveries", "unit": "MMcf/d", "priority": 3, "flow": "supply"}]),
    # L-099 (Erik: "dawn should be added here with the dawn pipeline"). Dawn's own data (Enbridge Gas's Gas Day Summary)
    # is behind a reCAPTCHA, so the box is the US gas landing there (gas_ont_supply's Dawn-side inflows).
    node("gas.ont.dawn", "Dawn hub (Enbridge Gas): US pipelines in at St. Clair and Ojibway", "hub", "dawn", None,
         value="supply.on.dawn_inflows_est", unit="MMcf/d", flow="supply",
         note=("Gas landing at Dawn off the US pipelines: Great Lakes (largely Canadian gas, from TC at Emerson) and "
               "Vector (incl. NEXUS gas) across the St. Clair River, and Panhandle at Ojibway (Windsor). Dawn's storage and its Dawn-Parkway sendout aren't public "
               "daily (Enbridge Gas's Gas Day Summary is behind a reCAPTCHA)."),
         panel=panel("supply.on.dawn_inflows_est", "glgt.st_clair.net_deliveries", "vector.st_clair.deliveries",
                     "pepl.ojibway.deliveries", "statcan.on.storage"),
         callout={"label": "Dawn: gas in from US pipelines (St. Clair + Ojibway, scheduled)", "priority": 2},
         callouts_extra=[{"depth": 1, "label": "Vector (incl. NEXUS)", "series": "vector.st_clair.deliveries",
                          "unit": "MMcf/d", "priority": 2, "flow": "supply"},
                         {"depth": 1, "label": "Great Lakes", "series": "glgt.st_clair.net_deliveries",
                          "unit": "MMcf/d", "priority": 2, "flow": "supply"},
                         {"depth": 1, "label": "Panhandle at Ojibway", "series": "pepl.ojibway.deliveries",
                          "unit": "MMcf/d", "priority": 3, "flow": "supply"}]),
    # L-106 8c (upne): Canadian gas into New England at East Hereford (PNGTS), as a dot
    node("gas.upne.entry.east_hereford", "East Hereford: TQM into PNGTS (upne entry from Québec)", "border",
         "east_hereford_kp", None, value="tcm.east_hereford.exports", unit="MMcf/d", flow="supply",
         panel=panel("tcm.east_hereford.exports", "tcm.philipsburg.exports", "tcm.qc.exports")),
    node("gas.ont.niagara", "Niagara Falls + Chippawa (US gas into Ontario)", "border", "niagara_falls", None,
         value="tcm.niagara.imports", unit="MMcf/d", panel=panel("tcm.niagara.imports"),
         callout={"label": "Niagara imports (scheduled)", "priority": 2}),
    node("gas.ont.waddington", "Waddington (TC Mainline → Iroquois, export to New York)", "border", "waddington", None,
         value="tcm.iroquois.exports", unit="MMcf/d", panel=panel("tcm.iroquois.exports", "tcm.eda.deliveries"),
         callout={"label": "Iroquois export (scheduled)", "priority": 2}),
]

AREA_META = {
    "USJR": ("gas.ngtl.usjr", "NGTL Upstream James River (receipts)", 1, "USJR receipts"),
    "WGAT": ("gas.ngtl.wgat", "NGTL West Gate (AB-BC / AB-MT borders)", 2, "West Gate"),
    "EGAT": ("gas.ngtl.egat", "NGTL East Gate (Empress / McNeill)", 1, "East Gate"),
    "OSDA": ("gas.ngtl.osda", "NGTL Oil Sands Delivery Area", 3, "Oil sands deliveries"),
    "NEDA": ("gas.ngtl.neda", "NGTL North & East Delivery Area", None, None),
    "FHBC": ("gas.ngtl.fhbc", "Foothills BC (takeaway from NGTL)", None, None),
    "FHSK": ("gas.ngtl.fhsk", "Foothills Saskatchewan (takeaway from NGTL)", None, None),
}
MW_NOTE = ("Midwest, kept sparse (L-095). Chicago drop-off (est., ±8.5 %) = Alliance + Northern Border's net deliveries "
           "at the Chicago-market points (Troy Grove, Manhattan, Joliet, Minooka, Will County, Channahon, North Hayden), "
           "less Aux Sable's receipts at Channahon (Alliance gas, so it isn't counted twice). Alliance's Chicago "
           "deliveries aren't public here, so its Canada receipts stand in for them (±10 %): the deliveries add its "
           "Tioga (Bakken) receipts and take off fuel and Aux Sable's liquids extraction. Northern Border and ANR are "
           "their Timely cycle (TC eConnects). State demand and storage are EIA's monthly figures, ~2 months behind.")
MW_DEMAND_NOTE = ("Demand is computed from weather (L-101): base + heating + cooling degree-day terms per state, "
                  "fitted to EIA's monthly deliveries Sep 2021 - Aug 2026 (Chicago, Milwaukee, Detroit temperatures). "
                  "Band = the monthly back-test's 90th percentile; tighter Nov-Mar, wider May-Sep. Citygate deliveries "
                  "include LDC storage injection, so they aren't demand.")
nodes += [
    # L-095 (Erik: "I really just care about whats getting dropped off at chicago with Chicago demand. Wisconsin total
    # demand, Michigan storage and demand, Bakken supply … a decent amount of these being only visible when I zoom in").
    # Overview: the Chicago drop-off and Bakken supply only; Wisconsin and Michigan from zoom 6.75; points from 7.5.
    node("gas.midwest.chicago", "Chicago: gas dropped off from the north (Alliance + Northern Border)", "system",
         "chicago", "schematic: Chicago-market value, anchored at " + P["chicago"][2], value="midwest.chicago.dropoff",
         # ±: Alliance's ±10 % (receipts standing in for its Chicago deliveries) on its ~83 % share (Reviewer, #381)
         unit="MMcf/d", band=0.085, flow="supply", region_value=True, always_show=True,
         note=MW_NOTE + " " + MW_DEMAND_NOTE,
         panel=panel("midwest.chicago.dropoff", "alliance.receipts.total", "nbpl.chicago.net_deliveries",
                     "demand.il.weather_est", "ngpl.chicago.citygate_deliveries", "eia.il.deliveries",
                     "anr.alliance.net_receipts"),
         callout={"label": "Chicago drop-off: Alliance + Northern Border (scheduled, est.)", "priority": 1},
         callouts_extra=[{"depth": 1, "band": 0.10, "label": "Alliance (est. from its Canada receipts)",
                          "series": "alliance.receipts.total", "unit": "MMcf/d", "priority": 1, "flow": "supply"},
                         {"depth": 1, "label": "Northern Border (Chicago points, net)",
                          "series": "nbpl.chicago.net_deliveries", "unit": "MMcf/d", "priority": 1, "flow": "supply"},
                         # depth 0 and last: a headline of its own, not a part of the drop-off (#355)
                         {"depth": 0, "band": 0.14, "label": "Illinois demand (est. from weather)",
                          "series": "demand.il.weather_est", "unit": "MMcf/d", "priority": 1},
                         {"depth": 1, "label": "Chicago LDC citygate (NGPL; includes storage injection)",
                          "series": "ngpl.chicago.citygate_deliveries", "unit": "MMcf/d", "priority": 2},
                         {"depth": 1, "label": "Illinois demand (EIA, monthly)", "series": "eia.il.deliveries",
                          "unit": "MMcf/d", "priority": 2}]),
    # L-106 8c (bakken): Northern Border leaving the Bakken eastbound, as a dot (the Bakken box shows the receipts)
    node("gas.bakken.exit.nbpl", "Northern Border past Glen Ullin (bakken exit, eastbound)", "border", "glen_ullin", None,
         value="nbpl.glen_ullin.flow", unit="MMcf/d", flow="supply",
         panel=panel("nbpl.glen_ullin.flow", "nbpl.bakken.net_receipts", "nbpl.port_of_morgan.receipts")),
    node("gas.midwest.bakken", "Bakken gas on Northern Border (Williston basin receipts, net)", "receipt",
         "watford_city", "schematic: Bakken value, anchored at " + P["watford_city"][2],
         value="nbpl.bakken.net_receipts", unit="MMcf/d", flow="supply", always_show=True, note=MW_NOTE,
         panel=panel("nbpl.bakken.net_receipts", "eia.nd.marketed_production", "nbpl.glen_ullin.flow",
                     "nbpl.port_of_morgan.receipts"),
         callout={"label": "Bakken on Northern Border (scheduled)", "priority": 1},
         callouts_extra=[{"depth": 0, "label": "North Dakota production (EIA, monthly)",
                          "series": "eia.nd.marketed_production", "unit": "MMcf/d", "priority": 1, "flow": "supply"}]),
    node("gas.midwest.wisconsin", "Wisconsin gas demand (est. from weather)", "system", "madison",
         "schematic: Wisconsin-wide value, anchored at " + P["madison"][2], value="demand.wi.weather_est",
         unit="MMcf/d", band=0.12, note=MW_NOTE + " " + MW_DEMAND_NOTE,
         panel=panel("demand.wi.weather_est", "eia.wi.deliveries", "anr.wisconsin.deliveries",
                     "anr.marshfield.viking_net_receipts"),
         callout={"label": "Wisconsin demand (est. from weather)", "priority": 2},
         callouts_extra=[{"depth": 1, "label": "ANR into Wisconsin (daily, scheduled)",
                          "series": "anr.wisconsin.deliveries", "unit": "MMcf/d", "priority": 2},
                         {"depth": 1, "label": "Wisconsin demand (EIA, monthly)", "series": "eia.wi.deliveries",
                          "unit": "MMcf/d", "priority": 2}]),
    node("gas.midwest.michigan", "Michigan gas demand (est. from weather) and storage", "system", "lansing",
         "schematic: Michigan-wide value, anchored at " + P["lansing"][2], value="demand.mi.weather_est",
         unit="MMcf/d", band=0.14, note=MW_NOTE + " " + MW_DEMAND_NOTE,
         panel=panel("demand.mi.weather_est", "eia.mi.deliveries", "eia.mi.storage.working_gas",
                     "glgt.farwell.net_deliveries", "glgt.muttonville.net_deliveries"),
         callout={"label": "Michigan demand (est. from weather)", "priority": 2},
         callouts_extra=[{"depth": 1, "label": "Michigan demand (EIA, monthly)", "series": "eia.mi.deliveries",
                          "unit": "MMcf/d", "priority": 2},
                         # depth 0 and last: storage is a headline of its own, not a part of demand (#355)
                         {"depth": 0, "label": "Michigan storage, working gas (EIA, month end)",
                          "series": "eia.mi.storage.working_gas", "unit": "Bcf", "priority": 2}]),
    # L-106 8c (umw): Great Lakes into Ontario at the Sault (umw exit, mbnwo entry), as a dot
    node("gas.umw.exit.sault", "Great Lakes into Ontario at the Sault (umw exit)", "border", "sault_mi", None,
         value="tcm.area.on.receipts", unit="MMcf/d", flow="supply",
         panel=panel("tcm.area.on.receipts", "tcm.area.on.outflow_est")),
    node("gas.glgt.farwell", "Farwell (Great Lakes ↔ ANR, by ANR's Michigan storage)", "interconnect", "farwell_mi",
         None, value="glgt.farwell.net_deliveries", unit="MMcf/d", flow="supply",
         panel=panel("glgt.farwell.net_deliveries", "glgt.emerson.net_receipts"),
         callout={"label": "Farwell: Great Lakes → ANR, net (scheduled)", "priority": 2}),     # L-099: a major point
    # L-099 (Erik: "boxes for the major spots … Just the major spots"): stored series only, zoomed in
    node("gas.nbpl.ventura", "Ventura (Northern Border → Northern Natural)", "interconnect", "ventura_ia", None,
         value="nbpl.ventura.net_deliveries", unit="MMcf/d", flow="supply",
         panel=panel("nbpl.ventura.net_deliveries", "nbpl.harper.flow", "nbpl.glen_ullin.flow"),
         callout={"label": "Ventura: Northern Border → Northern Natural, net (scheduled)", "priority": 3}),
    node("gas.viking.marshfield", "Marshfield (Viking → ANR)", "interconnect", "marshfield", None,
         value="anr.marshfield.viking_net_receipts", unit="MMcf/d", flow="supply",
         panel=panel("anr.marshfield.viking_net_receipts", "tcm.emerson.viking_deliveries"),
         callout={"label": "Marshfield: Viking → ANR, net (scheduled)", "priority": 3}),
    node("gas.glgt.muttonville", "Muttonville (Great Lakes ↔ ANR)", "interconnect", "richmond_mi",
         "schematic: Muttonville (St. Clair County), placed at " + P["richmond_mi"][2],
         value="glgt.muttonville.net_deliveries", unit="MMcf/d", flow="supply",
         panel=panel("glgt.muttonville.net_deliveries"),
         callout={"label": "Muttonville: Great Lakes → ANR, net (scheduled)", "priority": 3}),
]

areas = []
for a in TC_AREAS:
    acr = a["acronym"]
    if acr not in AREA_META or not a["locations"]:
        continue
    fid, name, prio, label = AREA_META[acr]
    ring = [[round(l["lng"], 4), round(l["lat"], 4)] for l in sorted(a["locations"], key=lambda l: l["id"])]
    ring.append(ring[0])
    props = {"id": fid, "kind": "area", "system": "gas", "type": "area", "name": name,
             "source": f"TC Energy NGTL DOP area '{acr}' outline: {TC_AREAS_URL}", "tc_area": acr,
             "label_point": [round(a["centerLng"], 4), round(a["centerLat"], 4)] if a["centerLat"] else None}
    if acr in ("USJR", "WGAT", "EGAT", "OSDA"):
        s = acr.lower()
        props.update(value=f"ngtl.dop.{s}.flow", capacity=f"ngtl.dop.{s}.capability", unit="MMcf/d",
                     panel=panel(f"ngtl.dop.{s}.capability", f"ngtl.dop.{s}.flow", f"ngtl.dop.{s}.flow_yago",
                                 events=fid))
    else:
        props["panel"] = panel(events=fid)
    if prio:
        props["callout"] = {"label": label, "priority": prio}
    if acr == "USJR":   # PLAN 5a: daily production estimate (GDSR x rd-measured rescale x last month's shares), ±0.7 %
        props["callouts_extra"] = [{"band": 0.007, "label": "production est.", "series": "ngtl.area.usjr.receipt_est",
                                    "unit": "MMcf/d", "priority": prio}]
        props["panel"]["series"] += ["ngtl.area.usjr.receipt_est", "ngtl.area.usjr.receipt"]
    if props["label_point"] is None:
        props.pop("label_point")
    areas.append({"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]}, "properties": props})

links = [
    link("gas.ngtl.north_montney", "NGTL North Montney Mainline", "NGTL", ["saturn", "groundbirch"],
         "North Montney Mainline from the Saturn area to Groundbirch"),
    link("gas.ngtl.groundbirch", "NGTL Groundbirch Mainline", "NGTL", ["groundbirch", "dawson_creek", "gordondale"],
         "Groundbirch Mainline east to the AB-BC border at Gordondale"),
    link("gas.ngtl.nw_mainline", "NGTL Northwest Mainline (Grande Prairie)", "NGTL",
         ["gordondale", "grande_prairie", "valleyview", "fox_creek", "edson"],
         "Grande Prairie Mainline from Gordondale south-east toward Edson"),
    link("gas.ngtl.edson", "NGTL Edson Mainline", "NGTL", ["edson", "usjr_kp"],
         "Edson Mainline to the Upstream of James River key point"),
    link("gas.ngtl.was", "NGTL Western Alberta System", "NGTL", ["usjr_kp", "calgary_kp", "west_gate"],
         "Western Alberta System south to the AB-BC border (West Gate)",
         capacity="ngtl.dop.wgat.capability"),
    link("gas.ngtl.eas", "NGTL Eastern Alberta System", "NGTL", ["usjr_kp", "carbon", "hanna", "east_gate"],
         "Eastern Alberta System east to the Empress/McNeill borders (East Gate)",
         capacity="ngtl.dop.egat.capability"),
    link("gas.ngtl.eas_empress", "NGTL to Empress", "NGTL", ["east_gate", "empress"],
         "East Gate to the Empress border", value="ngtl.csr.empress", unit="MMcf/d"),
    link("gas.ngtl.eas_mcneill", "NGTL to McNeill", "NGTL", ["east_gate", "mcneill"],
         "East Gate to the McNeill border", value="ngtl.csr.mcneill", unit="MMcf/d"),
    link("gas.ngtl.north", "NGTL north deliveries (Edmonton, oil sands)", "NGTL",
         ["usjr_kp", "edmonton", "north_east_kp", "osda_liege"],
         "NGTL north to the North & East and Oil Sands delivery areas",
         capacity="ngtl.dop.osda.capability"),
    # PLAN 4b item 7 (L-058): NGTL to every export border. These three have no separate daily flow (GDSR combines them in
    # "other borders"), so the lines carry no value; the border nodes carry the combined figure in their panel.
    link("gas.ngtl.ab_mt", "NGTL to the Alberta-Montana border", "NGTL", ["calgary_kp", "ab_mt_border"],
         "NGTL West Gate area south to the Alberta-Montana border interconnect (station 3868)"),
    link("gas.ngtl.cold_lake", "NGTL to the Cold Lake border", "NGTL", ["north_east_kp", "cold_lake_border"],
         "NGTL north-east to the Cold Lake border (Saskatchewan, station 1417)"),
    link("gas.ngtl.unity", "NGTL to the Unity border", "NGTL", ["hanna", "unity_border"],
         "NGTL Eastern Alberta System east to the Unity border (Saskatchewan, station 1250)"),
    # Foothills: the live flow is what NGTL hands over (CSR); capacity and the export throughput are CER key points
    # (gas_cer_keypoints, ~4 months behind).
    link("gas.foothills.bc", "Foothills BC", "Foothills", ["west_gate", "kingsgate"],
         "Foothills Zone 8 from the AB-BC border to Kingsgate", value="ngtl.csr.alberta_bc", unit="MMcf/d",
         capacity="cer.foothills.kingsgate.capacity",
         panel=panel("ngtl.csr.alberta_bc", "cer.foothills.kingsgate.throughput_q", "cer.foothills.kingsgate.capacity")),
    link("gas.foothills.sk", "Foothills Saskatchewan", "Foothills", ["mcneill", "monchy"],
         "Foothills Zone 9 from McNeill to Monchy", value="ngtl.csr.mcneill", unit="MMcf/d",
         capacity="cer.foothills.monchy.capacity",
         panel=panel("ngtl.csr.mcneill", "cer.foothills.monchy.throughput_q", "cer.foothills.monchy.capacity")),
    # Westcoast (Enbridge), whole: Fort Nelson Mainline + T-North to Station 2, T-South to Huntingdon, and FortisBC's take
    # at Huntingdon for the Lower Mainland. No daily public flow; the T-South capacity is Westcoast's daily posting, the
    # throughputs are CER key points (~4 months behind).
    # PLAN 6A: every Westcoast line carries its scheduled flow (gas_westcoast_oa, the latest cycle), signed in the
    # segment's direction (− = the other way); capacity is the operationally available capacity.
    link("gas.westcoast.fort_nelson", "Westcoast Fort Nelson → Station 2 (T-North)", "Westcoast (Enbridge)",
         ["fort_nelson", STN2], "Westcoast's Fort Nelson Mainline south to Station 2 (OA segment 'Fort Nelson to Stn 2'; "
         "the Aitken Creek Lateral joins it at CSN4)", value=W + "fort_nelson.scheduled",
         capacity=W + "fort_nelson.capacity", unit="MMcf/d",
         panel=panel(W + "fort_nelson.scheduled", W + "fort_nelson.capacity")),
    link("gas.westcoast.aitken_lateral", "Westcoast Aitken Creek Lateral", "Westcoast (Enbridge)",
         ["aitken_creek", STN2], "Westcoast's Aitken Creek Lateral (storage + lateral production) to the Fort Nelson line "
         "(OA segment 'Aitken Creek Lateral')", value=W + "aitken_lateral.scheduled",
         capacity=W + "aitken_lateral.capacity", unit="MMcf/d",
         panel=panel(W + "aitken_lateral.scheduled", W + "aitken_lateral.capacity")),
    link("gas.westcoast.t_north", "Westcoast Fort St. John → Station 2 (T-North)", "Westcoast (Enbridge)",
         ["fort_st_john", STN2], "Westcoast T-North from Fort St. John (CS1) west to Station 2 (OA segment 'Fort St John to "
         "Stn 2'; negative = eastbound)", value=W + "fort_st_john.scheduled", capacity=W + "fort_st_john.capacity",
         unit="MMcf/d",
         panel=panel(W + "fort_st_john.scheduled", W + "fort_st_john.capacity", "cer.westcoast.sunset_creek_east.throughput_q",
                     "cer.westcoast.gordondale_east.throughput_q", "ngtl.gdsr.groundbirch_east.flow",
                     "ngtl.gdsr.gordondale.flow")),
    link("gas.westcoast.alberta", "Westcoast 26\" Alberta line (to NGTL at Gordondale)", "Westcoast (Enbridge)",
         ["fort_st_john", "gordondale"], "Westcoast's 26-inch line from Fort St. John (CS1) east to the NGTL Gordondale "
         "interconnect (OA segments '26\" Alberta West/East'; value = East)", value=W + "alberta_east.scheduled",
         capacity=W + "alberta_east.capacity", unit="MMcf/d",
         panel=panel(W + "alberta_east.scheduled", W + "alberta_west.scheduled", W + "alberta_east.capacity",
                     "ngtl.gdsr.gordondale.flow")),
    link("gas.westcoast.t_south", "Westcoast T-South", "Westcoast (Enbridge)",
         [STN2, "prince_george", "williams_lake", "kamloops", "kingsvale", "huntingdon"],
         "Westcoast T-South from Station 2 to Huntingdon via Prince George, Williams Lake, Kamloops, Kingsvale",
         value=W + "station2_south.scheduled", capacity=W + "station2_south.capacity", unit="MMcf/d",
         panel=panel(W + "station2_south.scheduled", W + "station2_south.capacity", "westcoast.station2_south.capacity",
                     "cer.westcoast.huntingdon_export.throughput_q",
                     "cer.westcoast.fortisbc_lm.throughput_q", "cer.westcoast.kingsvale.throughput_q")),
    link("gas.fortisbc.lower_mainland", "FortisBC Lower Mainland (from Westcoast at Huntingdon)", "FortisBC",
         ["huntingdon", "vancouver"], "FortisBC's take from Westcoast at Huntingdon toward Vancouver (CER key point "
         "'FortisBC Lower Mainland'); capacity from the CER key point",
         capacity="cer.westcoast.fortisbc_lm.capacity", unit="MMcf/d",
         panel=panel("cer.westcoast.fortisbc_lm.throughput_q", "cer.westcoast.fortisbc_lm.capacity")),
    # TC Canadian Mainline from Empress to the Winnipeg junction (CER 'Northern Ontario Line' key point, Station 41), the
    # Emerson export leg, and (L-077, Erik: "fill out Sask, Manitoba, NOL/Ontario before NBJ") the Northern Ontario Line
    # to North Bay Junction. East of NBJ the flows are mostly Ontario storage and US imports (Dawn, Niagara, Chippawa);
    # only the EDA endpoint is kept, as a box. The prairie line keeps NGTL's measured Empress handover as its value;
    # the capabilities are TC's daily bulletin numbers (CER quarterly key points stay in the panels as dated context).
    link("gas.tc.mainline", "TC Canadian Mainline (Prairies): value = NGTL delivery at Empress", "TC Energy",
         ["prairies_kp", "moose_jaw", "regina", "brandon", "mainline_nol"],
         "TC Canadian Mainline from Empress (CER 'Prairies') via Moose Jaw, Regina and Brandon to the Winnipeg-area "
         "junction (CER 'Northern Ontario Line')", value="ngtl.csr.empress", unit="MMcf/d",
         capacity="tcm.prairies.capability",
         panel=panel("ngtl.csr.empress", "tcm.prairies.receipts", "tcm.prairies.capability",
                     "cer.mainline.prairies.throughput_q", "cer.mainline.prairies.capacity")),
    link("gas.tc.mainline.emerson", "TC Mainline to Emerson (US export, scheduled)", "TC Energy",
         ["mainline_nol", "emerson"],
         "TC Mainline south to the Emerson export points (CER 'Emerson I' + 'Emerson II')",
         value="tcm.emerson.deliveries", capacity="tcm.emerson.capability", unit="MMcf/d", flow="supply",
         panel=panel("tcm.emerson.deliveries", "tcm.emerson.capability", "cer.mainline.emerson.throughput_q",
                     "cer.mainline.emerson.capacity")),
    link("gas.tc.mainline.nol", "TC Mainline Northern Ontario Line (scheduled, est.)", "TC Energy",
         ["mainline_nol", "kenora", "dryden", "thunder_bay", "nipigon", "hearst", "kapuskasing", "cochrane", "nbj"],
         "Northern Ontario Line from Station 41 (CER 'Northern Ontario Line') via Kenora, Dryden, Thunder Bay, Nipigon, "
         "Hearst, Kapuskasing and Cochrane to Station 116 near North Bay (CER 'Eastern Triangle - NOL Receipts')",
         value="tcm.area.mb.outflow_est", capacity="tcm.nol.capability", unit="MMcf/d", band=0.06,
         panel=panel("tcm.area.mb.outflow_est", "tcm.nol.capability", "tcm.area.on.outflow_est",
                     "cer.mainline.nol.throughput_q", "cer.mainline.nol.capacity")),
    link("gas.westcoast.sunset_creek", "Westcoast Sunset Creek to NGTL", "Westcoast (Enbridge)",
         [STN2, "sunset_creek"], "Westcoast deliveries east toward NGTL at Sunset Creek (OA segment 'Sunset Creek "
         "Interconnect', sign flipped so + = to NGTL)", value=W + "sunset_creek.to_ngtl",
         capacity=W + "sunset_creek.capacity", unit="MMcf/d",
         panel=panel(W + "sunset_creek.to_ngtl", W + "sunset_creek.capacity", "ngtl.gdsr.groundbirch_east.flow")),
    # PLAN 4b item 3 (L-058): Alliance volumes. Zone 1 = Montney receipts upstream of the Blueberry compressor (NE BC +
    # NW AB); Zone 2 = Alberta receipts south of it (CER key point KP0045). montney/connectors/gas_alliance.py
    link("gas.alliance.zone1", ZONE1_LABEL, "Alliance (Pembina)",
         ["fort_st_john", "alliance_zone2"],
         "Alliance's Montney gathering area: NE BC receipt points down to the Blueberry compressor (Zone 2 key point)",
         value="alliance.zone1.receipts", capacity="alliance.zone1.capacity", unit="MMcf/d"),
    link("gas.alliance", "Alliance Pipeline", "Alliance (Pembina)",
         ["alliance_zone2", "valleyview", "fort_sask", "kerrobert", "alliance_border"],
         "Alliance from Zone 2 via the Edmonton area and Kerrobert to the US border near Elmore SK",
         value="alliance.receipts.total", capacity="alliance.capacity.total", unit="MMcf/d",
         panel=panel("alliance.receipts.total", "alliance.capacity.total", "cer.alliance.border.throughput_q",
                     "cer.alliance.border.capacity")),
    # L-093 (Erik: "complete the pipeline sprites for PGE, Socal, Mainline east (with the triangle) and st148 and upper
    # northeast TCPL mainline"). Schematic, node to node through cited points; values only where a stored series fits.
    # California: PG&E's Redwood path (Lines 400/401) and Baja path (Line 300); the lines into the LA basin.
    link("gas.pge.redwood", "PG&E Redwood path (Lines 400/401): Malin to the Bay Area", "PG&E",
         ["malin", "burney", "red_bluff", "williams_ca", "antioch"],
         "PG&E's backbone from the Malin receipt (GTN, Ruby) south via Burney, Red Bluff and Williams to Antioch "
         "(Bay Area)", value="pge.imports.north", unit="MMcf/d",
         panel=panel("pge.imports.north", "pge.imports.gtn", "pge.imports.ruby", "pge.demand.total")),
    link("gas.pge.baja", "PG&E Baja path (Line 300): Topock to the Bay Area", "PG&E",
         ["needles", "barstow", "mojave", "kettleman_city", "milpitas"],
         "PG&E's Line 300 from the Topock receipts (El Paso, Transwestern) west via Hinkley/Barstow, Mojave and Kettleman "
         "City to Milpitas", value="pge.imports.south", unit="MMcf/d",
         panel=panel("pge.imports.south", "pge.imports.el_paso", "pge.imports.transwestern", "pge.imports.kern_river")),
    # value = North Needles only (Reviewer on #375); SoCalGas's own Topock receipts (often 0) are in the panel
    link("gas.socal.needles", "Into the LA basin from North Needles", "SoCalGas", ["needles", "barstow",
         "victorville", "los_angeles"], "SoCalGas's northern system from North Needles (Transwestern) via Barstow and "
         "Victorville to Los Angeles", value="socal.imports.north_needles", unit="MMcf/d",
         panel=panel("socal.imports.north_needles", "socal.imports.topock_el_paso", "socal.imports.topock_transwestern")),
    link("gas.socal.ehrenberg", "Into the LA basin from Ehrenberg / Blythe", "SoCalGas",
         ["blythe", "desert_center", "indio", "moreno_valley", "los_angeles"],
         "SoCalGas's southern system from Ehrenberg (El Paso) and Blythe (North Baja) via Desert Center, Indio and Moreno "
         "Valley to Los Angeles", value="cal.imports.ehrenberg", unit="MMcf/d",
         panel=panel("cal.imports.ehrenberg", "socal.imports.ehrenberg", "socal.imports.blythe")),
    link("gas.socal.kramer", "Into the LA basin from Kramer Junction", "Kern River / Mojave → SoCalGas",
         ["boron", "adelanto", "los_angeles"], "Kern River / Mojave deliveries at Kramer Junction (by Boron) into "
         "SoCalGas via Adelanto to Los Angeles", value="socal.imports.kramer_junction", unit="MMcf/d",
         panel=panel("socal.imports.kramer_junction", "cal.imports.kramer")),
    link("gas.socal.wheeler_ridge", "Into the LA basin from Wheeler Ridge", "Kern River / Mojave → SoCalGas",
         ["mettler", "lebec", "santa_clarita", "los_angeles"], "Kern River / Mojave and PG&E deliveries at Wheeler Ridge "
         "(by Mettler) into SoCalGas over the Grapevine (Lebec) and Santa Clarita to Los Angeles",
         value="cal.imports.wheeler_ridge", unit="MMcf/d",
         panel=panel("cal.imports.wheeler_ridge", "socal.imports.wheeler_ridge", "socal.receipts.from_pge")),
    # TC Mainline east of North Bay Junction: the Eastern Triangle, Station 148 and beyond Montréal (TQM).
    link("gas.tc.mainline.barrie", "TC Mainline Barrie leg: North Bay Junction to Parkway", "TC Energy",
         ["nbj", "orillia", "barrie", "parkway_kp"],
         "the Eastern Triangle's western side, from North Bay Junction (Station 116) via Orillia and Barrie to Parkway "
         "(CER 'Eastern Triangle - Parkway Deliveries', Milton)"),
    link("gas.tc.mainline.niagara", "TC Mainline Niagara line: Niagara / Chippawa to Parkway", "TC Energy",
         ["niagara_kp", "parkway_kp"], "TC's Niagara line from the Niagara and Chippawa import points north to Parkway",
         value="tcm.niagara.imports", unit="MMcf/d", panel=panel("tcm.niagara.imports")),
    link("gas.tc.mainline.montreal", "TC Mainline Montreal Line: Parkway to Station 148", "TC Energy",
         ["parkway_kp", "toronto", "kingston", "iroquois_kp", "cornwall", "saint_lazare_kp"],
         "the Eastern Triangle's southern side, from Parkway via Toronto, Kingston, the Iroquois export and Cornwall to "
         "Station 148 at Les Cèdres (CER 'Saint Lazare + Sabrevois', where TQM leaves the Mainline)",
         panel=panel("tcm.parkway.net_deliveries", "tcm.iroquois.exports", "tcm.eda.deliveries")),
    link("gas.tc.mainline.shortcut", "TC Mainline North Bay Shortcut: North Bay Junction to Ottawa and Station 148",
         "TC Energy", ["nbj", "ottawa", "saint_lazare_kp"],
         "the Eastern Triangle's northern (upper northeast) side, from North Bay Junction east via Ottawa to the Montreal "
         "Line at Station 148", panel=panel("tcm.area.on.outflow_est", "tcm.eda.deliveries")),
    link("gas.tc.mainline.stn148", "TC Mainline downstream of Station 148: into Montréal (Énergir EDA)", "TC Energy",
         ["saint_lazare_kp", "montreal"], "the Mainline east of Station 148 (Les Cèdres) into Montréal, Énergir's "
         "Eastern Delivery Area; capacity = TC's weekly 'Downstream STN 148' capability",
         capacity="tcm.stn148.capability", unit="MMcf/d",
         panel=panel("tcm.stn148.capability", "tcm.energir.deliveries", "tcm.eda.deliveries")),
    link("gas.tc.iroquois", "Iroquois export (Waddington, NY)", "Iroquois Gas Transmission",
         ["iroquois_kp", "waddington"], "the Iroquois interconnect at the border by Iroquois, ON, to Waddington, NY",
         value="tcm.iroquois.exports", unit="MMcf/d", panel=panel("tcm.iroquois.exports")),
    link("gas.tqm.quebec", "TQM: Station 148 to Montréal, Trois-Rivières and Québec", "TQM (TC Energy / Énergir)",
         ["saint_lazare_kp", "montreal", "trois_rivieres", "quebec_city"],
         "Trans Québec & Maritimes from the Mainline at Saint-Lazare along the north shore via Montréal and "
         "Trois-Rivières to Québec", panel=panel("tcm.energir.deliveries", "demand.qc.implied_est")),
    link("gas.tqm.east_hereford", "TQM to East Hereford (PNGTS export)", "TQM (TC Energy / Énergir)",
         ["montreal", "east_hereford_kp"], "TQM's lateral from the Montréal area to the PNGTS border point at East "
         "Hereford", panel=panel("tcm.qc.exports")),
    link("gas.tc.qc_exports_south", "Philipsburg / Napierville exports", "TC Energy", ["montreal", "other_us_ne_kp"],
         "TC's deliveries south of Montréal to the Vermont and New York border points (CER 'Other US Northeast')",
         panel=panel("tcm.qc.exports")),
    # L-094 (Erik: "add Kern, the rest of NWPL, and the remaining sections of the rockies"). Schematic; one value per
    # line, stored series only, no new requests.
    link("gas.kern.river", "Kern River (incl. Mojave): Opal to California (Kramer, Wheeler Ridge)",
         "Kern River (Berkshire)",
         ["opal", "salt_lake_city", "beaver_ut", "st_george", "las_vegas", "barstow", "boron", "mettler"],
         "Kern River from the Opal hub via Salt Lake City, Beaver, St. George and Las Vegas into California at Daggett "
         "(by Barstow), Kramer Junction (by Boron) and Wheeler Ridge (by Mettler)", value="cal.imports.kern_mojave",
         unit="MMcf/d", panel=panel("cal.imports.kern_mojave", "cal.imports.kramer", "cal.imports.wheeler_ridge")),
    link("gas.nwp.rockies", "Northwest Pipeline: Opal to Stanfield (Idaho, Baker)", "Northwest Pipeline (Williams)",
         ["opal", "kemmerer", "soda_springs", "pocatello", "twin_falls", "boise", "baker_city", "stanfield"],
         "NWP's mainline from the Opal / Kemmerer area via Soda Springs, Pocatello, Twin Falls and Boise to Baker City "
         "and Stanfield; value = NWP's net flow north at Baker (the Rockies side into the PacNW; − = PacNW gas east)",
         value="nwp.baker.net_north", unit="MMcf/d", panel=panel("nwp.baker.net_north", "pnw.imports.total")),
    link("gas.nwp.ignacio", "Northwest Pipeline south leg: Ignacio to Green River and Opal", "Northwest Pipeline "
         "(Williams)", ["ignacio", "rangely", "green_river", "opal"], "NWP's southern leg from the San Juan basin at "
         "Ignacio via Rangely and Green River to the Opal area (no public flow kept here)"),
    link("gas.ruby", "Ruby Pipeline: Opal to Malin", "Ruby (Tallgrass / Kinder Morgan)",
         ["opal", "wells_nv", "winnemucca", "malin"], "Ruby from the Opal hub across northern Nevada (Wells, "
         "Winnemucca) to Malin; value = PG&E's receipts from Ruby at Malin (Onyx Hill)",
         value="pge.imports.ruby", unit="MMcf/d", panel=panel("pge.imports.ruby", "pge.imports.north")),
    # L-095 Midwest (Erik: "add GLGT, with farwell and muttonville …, Viking, ANR, NBPL"). Schematic, through Census place
    # points; one value per line from the few requests (NBPL, ANR) or existing ones (GLGT, TC's Emerson).
    link("gas.nbpl.north", "Northern Border: Port of Morgan through the Bakken to Ventura", "Northern Border (TC Energy)",
         ["monchy", "glasgow_mt", "watford_city", "killdeer", "glen_ullin", "aberdeen_sd", "watertown_sd",
          "marshall_mn", "ventura_ia"],
         "Northern Border from Port of Morgan (Monchy) via Glasgow, the Bakken receipts (Watford City, Killdeer) and "
         "Glen Ullin, then Aberdeen, Watertown and Marshall to Ventura, IA; value = the flow past Glen Ullin",
         value="nbpl.glen_ullin.flow", unit="MMcf/d",
         panel=panel("nbpl.glen_ullin.flow", "nbpl.bakken.net_receipts", "nbpl.port_of_morgan.receipts",
                     "nbpl.ventura.net_deliveries")),
    link("gas.nbpl.chicago", "Northern Border Chicago leg: Ventura to Joliet", "Northern Border (TC Energy)",
         ["ventura_ia", "marshalltown", "harper_ia", "davenport", "princeton_il", "joliet"],
         "Northern Border's Chicago Project from Ventura via Marshalltown, Harper and Davenport into Illinois "
         "(Princeton) to the Joliet area; value = the flow past Harper",
         value="nbpl.harper.flow", unit="MMcf/d", panel=panel("nbpl.harper.flow", "nbpl.chicago.net_deliveries")),
    link("gas.glgt.west", "Great Lakes: Emerson to Crystal Falls", "Great Lakes Gas Transmission (TC Energy)",
         ["emerson", "thief_river_falls", "bemidji", "grand_rapids_mn", "carlton_mn", "ironwood", "crystal_falls"],
         "Great Lakes from the TC Mainline at Emerson (Emerson 2) via Thief River Falls, Bemidji, Grand Rapids, "
         "Carlton (Duluth) and Ironwood to Crystal Falls (ANR at Fortune Lake); value = its net receipts at Emerson",
         value="glgt.emerson.net_receipts", unit="MMcf/d",
         panel=panel("glgt.emerson.net_receipts", "tcm.emerson.glgt_deliveries")),
    link("gas.glgt.east", "Great Lakes: Crystal Falls to Farwell and St. Clair", "Great Lakes Gas Transmission "
         "(TC Energy)", ["crystal_falls", "manistique", "st_ignace", "gaylord", "farwell_mi", "richmond_mi",
                         "st_clair_mi"],
         "Great Lakes across the Upper Peninsula (Manistique), the Straits (St. Ignace) and Gaylord to the ANR "
         "interconnects at Farwell and Muttonville and on to St. Clair (Ontario); value = net into Ontario at St. Clair",
         value="glgt.st_clair.net_deliveries", unit="MMcf/d", flow="supply",
         panel=panel("glgt.st_clair.net_deliveries", "glgt.farwell.net_deliveries", "glgt.muttonville.net_deliveries")),
    # L-099: Dawn's pipelines. Schematic; values only where a stored series is the line's flow.
    link("gas.vector", "Vector: Joliet to Dawn", "Vector Pipeline (Enbridge / DTE)",
         ["joliet", "highland_in", "athens_mi", "milford_mi", "marine_city", "courtright", "dawn"],
         "Vector from the Joliet hub via Highland (IN), Athens and Milford (MI) to the St. Clair River by Marine City, "
         "across to Courtright and Dawn; value = its deliveries into Canada at St. Clair (NEXUS gas included)",
         value="vector.st_clair.deliveries", unit="MMcf/d", flow="supply", panel=panel("vector.st_clair.deliveries")),
    link("gas.glgt.dawn", "Great Lakes into Ontario: St. Clair to Dawn", "Great Lakes Gas Transmission (TC Energy) → "
         "Enbridge Gas", ["st_clair_mi", "courtright", "dawn"],
         "Great Lakes' St. Clair delivery across the river to Courtright and on to Dawn; value = net into Ontario",
         value="glgt.st_clair.net_deliveries", unit="MMcf/d", flow="supply",
         panel=panel("glgt.st_clair.net_deliveries")),
    link("gas.ont.ojibway", "Panhandle into Ontario: Ojibway to Dawn", "Panhandle Eastern → Enbridge Gas",
         ["windsor_on", "dawn"], "Panhandle's Detroit River crossing at Ojibway (Windsor) and Enbridge Gas's line on to "
         "Dawn; value = Panhandle's deliveries at Ojibway", value="pepl.ojibway.deliveries", unit="MMcf/d",
         flow="supply", panel=panel("pepl.ojibway.deliveries")),
    link("gas.ont.dawn_parkway", "Enbridge Gas Dawn-Parkway: Dawn to Parkway", "Enbridge Gas",
         ["dawn", "london_on", "kirkwall_on", "milton"],
         "Enbridge Gas's Dawn-Parkway system from Dawn via London and Kirkwall to Parkway (Milton), where it meets the "
         "TC Mainline (no public daily flow: Enbridge Gas's Gas Day Summary is behind a reCAPTCHA)",
         panel=panel("tcm.parkway.net_deliveries", "tcm.kirkwall.net_deliveries", "supply.on.dawn_inflows_est")),
    link("gas.viking", "Viking: Emerson to Marshfield", "Viking Gas Transmission (ONEOK)",
         ["emerson", "hallock", "frazee", "chisago_city", "marshfield"],
         "Viking from the TC Mainline at Emerson (Emerson 1) via Hallock, Frazee and Chisago City to ANR at "
         "Marshfield, WI; value = TC's deliveries to Viking", value="tcm.emerson.viking_deliveries", unit="MMcf/d",
         panel=panel("tcm.emerson.viking_deliveries", "anr.marshfield.viking_net_receipts")),
    link("gas.anr.wisconsin", "ANR: Joliet to Wisconsin and Crystal Falls", "ANR Pipeline (TC Energy)",
         ["joliet", "sandwich_il", "janesville", "kewaskum", "weyauwega", "crystal_falls"],
         "ANR's northern leg from the Joliet hub (Alliance) via Sandwich, Janesville, Kewaskum and Weyauwega to "
         "Great Lakes at Crystal Falls; value = ANR's deliveries into Wisconsin", value="anr.wisconsin.deliveries",
         unit="MMcf/d", panel=panel("anr.wisconsin.deliveries", "anr.alliance.net_receipts")),
    link("gas.anr.marshfield", "ANR: Weyauwega to Marshfield (Viking)", "ANR Pipeline (TC Energy)",
         ["weyauwega", "stevens_point", "marshfield"],
         "ANR west via Stevens Point to the Viking interconnect at Marshfield; value = ANR's net receipts from Viking",
         value="anr.marshfield.viking_net_receipts", unit="MMcf/d",
         panel=panel("anr.marshfield.viking_net_receipts", "tcm.emerson.viking_deliveries")),
    link("gas.anr.michigan", "ANR: Sandwich to Michigan storage and Farwell", "ANR Pipeline (TC Energy)",
         ["sandwich_il", "bridgman", "big_rapids", "farwell_mi"],
         "ANR's Southwest Mainline from Sandwich via Bridgman into Michigan, north by Big Rapids (Woolfolk) to its "
         "storage fields and Great Lakes at Farwell (no public flow kept here)",
         panel=panel("eia.mi.storage.working_gas", "glgt.farwell.net_deliveries")),
    # L-085: NWP and GTN (schematic, through Census place points)
    link("gas.nwp.north", "Northwest Pipeline: Sumas to Jackson Prairie", "Northwest Pipeline (Williams)",
         ["sumas", "mount_vernon", "snohomish", "sumner", "jackson_prairie"],
         "NWP's I-5 corridor from the Sumas receipt (Westcoast at Huntingdon) via Mount Vernon, Snohomish and Sumner "
         "to Jackson Prairie", value="nwp.sumas.receipts", capacity="nwp.sumas.capacity", unit="MMcf/d",
         panel=panel("nwp.sumas.receipts", "nwp.sumas.capacity", "nwp.jackson_prairie.net_withdrawal")),
    link("gas.nwp.columbia", "Northwest Pipeline: Stanfield to Jackson Prairie", "Northwest Pipeline (Williams)",
         ["stanfield", "goldendale", "washougal", "jackson_prairie"],
         "NWP from the GTN interconnect at Stanfield along the Columbia (Goldendale, Washougal) north to Jackson "
         "Prairie; value = the gas NWP takes from GTN at Stanfield", value="nwp.stanfield.from_gtn", unit="MMcf/d",
         panel=panel("nwp.stanfield.from_gtn", "gtn.stanfield.to_nwp")),
    link("gas.gtn.north", "Gas Transmission Northwest: Kingsgate to Stanfield", "GTN (TC Energy)",
         ["kingsgate", "bonners_ferry", "sandpoint", "rathdrum", "rosalia", "starbuck", "stanfield"],
         "GTN from Kingsgate via Bonners Ferry, Sandpoint, the Spokane area (Rathdrum, Rosalia) and Starbuck to "
         "Stanfield", value="gtn.kingsgate.receipts", capacity="gtn.kingsgate.capacity", unit="MMcf/d",
         panel=panel("gtn.kingsgate.receipts", "gtn.kingsgate.capacity", "gtn.local.deliveries")),
    link("gas.gtn.south", "Gas Transmission Northwest: Stanfield to Malin", "GTN (TC Energy)",
         ["stanfield", "madras", "bend", "chemult", "malin"],
         "GTN from Stanfield via Madras, Bend and Chemult to Malin (California border)",
         value="gtn.malin.deliveries", capacity="gtn.malin.capacity", unit="MMcf/d", flow="supply",
         panel=panel("gtn.malin.deliveries", "gtn.malin.capacity", "gtn.tuscarora.deliveries")),
    link("gas.cgl", "Coastal GasLink", "Coastal GasLink (TC Energy)",
         ["wilde_lake", "chetwynd", "vanderhoof", "burns_lake", "houston", "kitimat"],
         "Coastal GasLink from the Willow Valley Interconnect (Wilde Lake) to LNG Canada at Kitimat",
         value="ngtl.csr.willow_valley", value_kind="ngtl_share", unit="MMcf/d"),
]

for fname, feats in (("nodes", nodes), ("areas", areas), ("links", links)):
    fc = {"type": "FeatureCollection", "features": feats}
    (OUT / f"{fname}.geojson").write_text(json.dumps(fc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(fname, len(feats))
