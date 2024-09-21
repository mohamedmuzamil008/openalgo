#Mapping OpenAlgo API Request https://openalgo.in/docs
#Mapping Zerodha Broking Parameters https://kite.trade/docs/connect/v3/

from database.token_db import get_br_symbol, get_token
import os

def map_order_type(pricetype):
    """
    Maps the new pricetype to the existing order type.
    """
    order_type_mapping = {
        "MARKET": "MARKET",
        "LIMIT": "LIMIT",
        "SL": "STOP_LOSS",
        "SL-M": "SL_MARKET"
    }
    return order_type_mapping.get(pricetype, "MARKET")  # Default to MARKET if not found

def map_product_type(product):
    """
    Maps the new product type to the existing product type.
    """
    product_type_mapping = {
        "CNC": "DELIVERY",
        "NRML": "NORMAL",
        "MIS": "INTRADAY"
    }
    return product_type_mapping.get(product, "MIS")  # Default to INTRADAY if not found

def reverse_map_product_type(product):
    """
    Reverse maps the broker product type to the OpenAlgo product type, considering the exchange.
    """
    reverse_product_type_mapping = {
        "DELIVERY": "CNC",
        "NORMAL": "NRML",
        "INTRADAY": "MIS"
    }
    return reverse_product_type_mapping.get(product, "INTRADAY")

def transform_data(data):
    """
    Transforms the new API request structure to the current expected structure.
    """
    pseudo_account_id = os.getenv('PSEUDO_ACCOUNT_ID')

    transformed = {
        "pseudoAccount": pseudo_account_id,      
        "exchange": "NSE",
        "symbol": data['symbol'],
        "tradeType": data['action'].upper(),
        "orderType": map_order_type(data["pricetype"]),
        "productType": map_product_type(data["product"]),
        "quantity": data["quantity"],
        "price": data.get("price", "0"),
        "triggerPrice": data.get("trigger_price", "0")
    }   
    return transformed

def transform_cover_order_data(data):
    """
    Transforms the new API request structure to the current expected structure.
    """
    pseudo_account_id = os.getenv('PSEUDO_ACCOUNT_ID')

    transformed = {
        "pseudoAccount": pseudo_account_id,      
        "exchange": "NSE",
        "symbol": data['symbol'],
        "tradeType": data['action'].upper(),
        "orderType": map_order_type(data["pricetype"]),
        "quantity": data["quantity"],
        "price": data.get("price", "0"),
        "triggerPrice": data["trigger_price"]
    }   
    return transformed


def transform_modify_order_data(data):
    pseudo_account_id = os.getenv('PSEUDO_ACCOUNT_ID')
    return {
        "pseudoAccount": pseudo_account_id, 
        "platformId": data.get('orderid'),
        "orderType": map_order_type(data.get("pricetype")) if data.get("pricetype") != "" else "",
        "quantity": int(data.get("quantity"), "0"),
        "price": data.get("price", "0"),
        "triggerPrice": data.get("trigger_price", "0")
    }


