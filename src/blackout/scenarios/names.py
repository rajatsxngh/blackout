"""Name pools for generated worlds: places, facilities and the people on the radio.

All names are invented. Pools are tuples, so draws from them are deterministic.
"""

HOSPITALS = (
    "St. Mary's Hospital",
    "Riverside Clinic",
    "Mercy General Hospital",
    "Lakeview Medical Center",
    "Northgate Hospital",
    "Good Samaritan Hospital",
    "Hillcrest Hospital",
    "County General Hospital",
    "Bayside Clinic",
    "St. Jude's Hospital",
)

DEPOTS = (
    "Northside Fuel Depot",
    "Harbor Fuel Depot",
    "Eastgate Depot",
    "Southfield Fuel Yard",
    "Ironworks Depot",
    "Westpoint Fuel Depot",
)

SUBSTATIONS = (
    "Ashford Substation",
    "Ridgeway Substation",
    "Millbrook Substation",
    "Foundry Substation",
    "Kingsley Substation",
    "Quarry Substation",
    "Linden Substation",
)

LANDMARKS = (
    "City Hall",
    "Riverside Park",
    "Union Station",
    "Memorial Stadium",
    "Central Library",
    "the Courthouse",
    "the Water Tower",
    "Lincoln High",
    "the Farmers Market",
    "St. Paul's Church",
    "the Fire Station",
    "Grand Theater",
)
"""Landmarks that need an article carry it, so they read naturally mid-sentence."""

ZONES_3X3 = (
    ("Northwest", "North", "Northeast"),
    ("West", "Downtown", "East"),
    ("Southwest", "South", "Southeast"),
)
"""Zone names by block row, then column (3x3 blocks; other layouts are numbered)."""

SURNAMES = (
    "Diaz",
    "Okafor",
    "Chen",
    "Novak",
    "Patel",
    "Brennan",
    "Kowalski",
    "Haddad",
    "Silva",
    "Murphy",
    "Tanaka",
    "Reyes",
    "Lindqvist",
    "Abara",
    "Moreau",
    "Fischer",
)

HOSPITAL_ROLES = ("Charge nurse", "Facilities manager", "Duty manager", "Nurse supervisor")
DEPOT_ROLES = ("Depot supervisor", "Yard manager", "Fuel dispatcher")
POLICE_RANKS = ("Officer", "Officer", "Sgt.")
PUBLIC_SOURCES = (
    "Caller on the public line",
    "Resident, 311 line",
    "Text tip line",
    "Caller on the non-emergency line",
)
GRID_CONTROL = "Grid control center"

AUTHORITIES = (
    "the Mayor's office",
    "City Emergency Management",
    "the Grid Operations Director",
    "the Fire Chief",
    "the Governor's emergency desk",
    "the Utility Commission",
)
"""Who an L3 injection claims to speak for. It always arrives on an unverified public line."""
