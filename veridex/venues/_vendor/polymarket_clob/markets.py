import numpy as np

'''
The (standard) data types recommended for each information type.
'''

TIMESTAMP = "timestamp"

TICKER = "ticker"
AMOUNT = "amount"
PRICE = "price"
ENTRY = "entry"
VALUE = "value"
ACCOUNT_EQUITY = "equity_total"

#order
LIMIT_PRICE = "price"
ORDER_ID = "oid"
ORDER_AMOUNT = "amount"
ORDER_FILLED_SIZE = "filled_sz"

ORDER_TYPE = "ord_type"

TIME_IN_FORCE = "tif"
TIME_IN_FORCE_GTC = 'GTC' #good till cancel
TIME_IN_FORCE_IOC = 'IOC' #immediate or cancel
TIME_IN_FORCE_FOK = 'FOK' #fill or kill
TIME_IN_FORCE_GTD = 'GTD' #good till date
TIME_IN_FORCE_MOC = 'MOC' #market on close
TIME_IN_FORCE_MOO = 'MOO' #market on open
TIME_IN_FORCE_ALO = "ALO" #add liquidity only

from decimal import Decimal
types = {
    TIMESTAMP : int, #ms

    TICKER : str,
    AMOUNT : Decimal,
    PRICE : Decimal,
    ENTRY : Decimal,
    VALUE : Decimal,
    ACCOUNT_EQUITY : float,

    LIMIT_PRICE : Decimal,
    ORDER_ID : str,
    ORDER_AMOUNT : Decimal,
    ORDER_FILLED_SIZE : Decimal,

    ORDER_TYPE: str,

    TIME_IN_FORCE : str,
    TIME_IN_FORCE_GTC : str,
    TIME_IN_FORCE_IOC : str,
    TIME_IN_FORCE_FOK : str,
    TIME_IN_FORCE_GTD : str,
    TIME_IN_FORCE_MOC : str,
    TIME_IN_FORCE_MOO : str,
    TIME_IN_FORCE_ALO : str,
}

def standard_types(dct):
    for k,v in dct.items():
        if isinstance(v,dict):
            dct[k] = standard_types(v)
        if k in types and v not in [None, np.nan]:
            if types[k] == Decimal:
                try:
                    dct[k] = types[k](str(v))
                except:
                    dct[k] = np.nan
            else:
                dct[k] = types[k](v)

    return dct
