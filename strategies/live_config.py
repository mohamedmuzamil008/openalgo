# strategies/live_config.py
"""Configuration for Live Trading Engine"""

import os
from dotenv import load_dotenv
from datetime import time

load_dotenv()  # Load .env file
# API Configuration
API_CONFIG = {
    'api_key': os.getenv('APP_KEY'),
    'host': os.getenv('HOST_SERVER'),
    'ws_url': os.getenv('WEBSOCKET_URL')
}

# Database Configuration
DB_CONFIG = {
    'user': os.getenv('TIMESCALE_DB_USER'),
    'password': os.getenv('TIMESCALE_DB_PASSWORD'),
    'host': os.getenv('TIMESCALE_DB_HOST'),
    'port': os.getenv('TIMESCALE_DB_PORT'),
    'dbname': os.getenv('TIMESCALE_DB_NAME_LIVE')
}

# Trading Configuration
TRADING_CONFIG = {
    # Market Hours (IST)
    'market_start': time(9, 15),
    'market_end': time(15, 30),
    'trading_start': time(9, 20),  # Start 5 min after market open
    'trading_end': time(15, 10),   # Stop 20 min before close
    
    # Position Sizing
    'capital': 10000,
    'leverage': 5,
    'capital_alloc_pct': 30,  # 30% per trade
    
    # Risk Management
    'max_open_positions': 3,
    'max_daily_trades': 5,
    'max_strategy_trades_per_day': 1,
    
    # Trailing Stop
    'trail_activation_pct': 0.9,  # Activate at 0.5% profit
    'trail_stop_gap_pct': 0.2,
    'trail_increment_pct': 0.2,
    'sl_pct': 1.5,
    'tp_pct': 1.5,    
    
    # Scanning
    'scan_frequency': 60,    # seconds
    'monitor_frequency': 60, # seconds
}

# Symbols Configuration
SYMBOL_FILES = {
    'live': 'strategies/symbol_list_live.csv',
    'test': 'strategies/symbol_list_test.csv'  # Smaller list for testing
}

# Logging Configuration
LOG_CONFIG = {
    'level': 'INFO',  # Default level
    'levels': ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],  # All available levels
    'format': '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    'file_pattern': 'live_trading_{date}.log',
    'console_level': 'INFO',  # Level for console output
    'file_level': 'DEBUG'    # Level for file output
}
