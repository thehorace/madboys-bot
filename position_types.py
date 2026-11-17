"""Only explicit position strings from EA's per-match `pos` field are decoded.

No numeric codes, archetypes or favourite-position fields imply a match slot.
"""
EA_ROLES = {'goalkeeper':'GK','defender':'DEF','midfielder':'MID','forward':'FWD'}
EXACT_ROLES = {'GK':'GK','RB':'DEF','CB':'DEF','LB':'DEF','RWB':'DEF','LWB':'DEF',
               'CDM':'MID','CM':'MID','CAM':'MID','RM':'MID','LM':'MID','RAM':'MID','LAM':'MID',
               'RW':'FWD','LW':'FWD','ST':'FWD','CF':'FWD'}
ROLE_NAMES = {v:k for k,v in EA_ROLES.items()}

def ea_position(value):
    """Return (broad role, exact slot), without guessing unfamiliar values."""
    if not isinstance(value,str):
        return None,None
    text=value.strip()
    if text.upper() in ('DEF','MID','FWD'):
        return text.upper(),None
    if text.lower() in EA_ROLES:
        role=EA_ROLES[text.lower()]
        return role,'GK' if role=='GK' else None
    exact=text.upper()
    if exact in EXACT_ROLES:
        return EXACT_ROLES[exact],exact
    return None,None
