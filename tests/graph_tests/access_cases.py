"""The access rule's cases, shared by the pure test and the Neo4j parity test.

Each case: name, visibility, aclGroups, viewer, readable as a node, readable as an edge.
"""

MEMBER = {"member": True, "principals": ["user:sam@acme.com", "domain:acme.com", "group:g:auth"]}
OUTSIDER = {"member": False, "principals": ["user:eve@other.com", "domain:other.com"]}

CASES = [
    ("public, outsider", "public", None, OUTSIDER, True, True),
    ("internal, member", "internal", None, MEMBER, True, True),
    ("internal, outsider", "internal", None, OUTSIDER, False, False),
    ("restricted, in the group", "restricted", ["group:g:auth"], MEMBER, True, True),
    ("restricted, named user", "restricted", ["user:eve@other.com"], OUTSIDER, True, True),
    ("restricted, not in the group", "restricted", ["group:g:auth"], OUTSIDER, False, False),
    ("restricted, member but not in the group", "restricted", ["group:g:finance"], MEMBER, False, False),
    ("restricted, no groups", "restricted", None, MEMBER, False, False),
    ("no visibility", None, None, MEMBER, False, True),
    ("no visibility, outsider", None, None, OUTSIDER, False, True),
]
