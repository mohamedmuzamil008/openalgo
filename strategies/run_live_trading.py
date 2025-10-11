#!/usr/bin/env python3
# strategies/run_live_trading.py
"""Startup script for Live Trading Engine"""

import sys
import os
import logging
import argparse
import pandas as pd
from datetime import datetime, time
import signal
import time as time_module
import pytz

# Add project root directory to path
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)  # Parent directory of strategies
sys.path.append(project_root)

from live_trading_engine import LiveTradingEngine
from live_config import API_CONFIG, DB_CONFIG, TRADING_CONFIG, SYMBOL_FILES, LOG_CONFIG

def setup_logging(debug=False):
    """Setup logging configuration with different levels for console and file output"""
    # Create log directory if it doesn't exist
    log_dir = os.path.join(project_root, 'log')
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
        
    # Get the log file name with full path
    log_file = os.path.join(log_dir, LOG_CONFIG['file_pattern'].format(
        date=datetime.now().strftime("%Y%m%d")
    ))
    
    # Create logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG if debug else logging.INFO)  # Set to lowest level to capture everything
    
    # Clear any existing handlers
    root_logger.handlers = []
    
    # Create formatters
    formatter = logging.Formatter(LOG_CONFIG['format'])
    
    # File Handler (captures all levels by default)
    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(getattr(logging, LOG_CONFIG.get('file_level', 'INFO')))
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)
    
    # Console Handler (for terminal output)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(getattr(logging, LOG_CONFIG.get('console_level', 'INFO')))
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)
    
    # Reduce noise from other libraries
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('requests').setLevel(logging.WARNING)
    
    # Log the configuration
    logging.info("Logging system initialized")
    logging.debug(f"File logging level: {LOG_CONFIG.get('file_level', 'INFO')}")
    logging.debug(f"Console logging level: {LOG_CONFIG.get('console_level', 'INFO')}")
    if debug:
        logging.info("Debug mode enabled - capturing all log levels")

def load_symbols(symbol_file):
    """Load symbols from CSV file"""
    try:
        if not os.path.exists(symbol_file):
            raise FileNotFoundError(f"Symbol file not found: {symbol_file}")
        
        symbols_df = pd.read_csv(symbol_file)
        symbols = symbols_df['Symbol'].tolist()
        
        logging.info(f"Loaded {len(symbols)} symbols from {symbol_file}")
        return symbols
        
    except Exception as e:
        logging.error(f"Error loading symbols: {e}")
        sys.exit(1)

def validate_config():
    """Validate configuration"""
    errors = []
    
    # Check database config
    required_db_fields = ['user', 'password', 'host', 'port', 'dbname']
    for field in required_db_fields:
        if not DB_CONFIG.get(field):
            errors.append(f"Missing database config: {field}")
    
    # Check API config
    if not API_CONFIG.get('api_key'):
        errors.append("Missing API key")
    if not API_CONFIG.get('host'):
        errors.append("Missing API host")
    
    if errors:
        for error in errors:
            logging.error(f"Config Error: {error}")
        sys.exit(1)
    
    logging.info("Configuration validated")

def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description='Live Trading Engine')
    parser.add_argument('--symbols', choices=['live', 'test'], default='live',
                       help='Symbol list to use (live or test)')
    parser.add_argument('--debug', action='store_true',
                       help='Enable debug logging')
    parser.add_argument('--dry-run', action='store_true',
                       help='Dry run mode (no actual orders)')
    parser.add_argument('--max-symbols', type=int,
                       help='Limit number of symbols (for testing)')
    
    args = parser.parse_args()
    
    # Setup logging
    setup_logging(debug=args.debug)
    logger = logging.getLogger("Startup")
    
    logger.info("Starting Live Trading Engine")
    logger.info(f"Mode: {'DRY RUN' if args.dry_run else 'LIVE TRADING'}")
     
    # Validate configuration
    validate_config()
    
    # Load symbols
    symbol_file = SYMBOL_FILES[args.symbols]
    symbols = load_symbols(symbol_file)
    
    # Limit symbols if requested
    if args.max_symbols:
        symbols = symbols[:args.max_symbols]
        logger.info(f"Limited to {len(symbols)} symbols for testing")
    
    # Global engine reference for signal handler
    engine = None
    
    def signal_handler(signum, frame):
        logger.info("Shutdown signal received")
        if engine:
            engine.stop()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        # Create and configure engine
        engine = LiveTradingEngine(
            api_key=API_CONFIG['api_key'],
            host=API_CONFIG['host'],
            ws_url=API_CONFIG['ws_url'],
            db_config=DB_CONFIG,
            symbols=symbols
        )        
                
        # Set dry run mode
        if args.dry_run:
            logger.info(" DRY RUN MODE: No actual orders will be placed")
            # You can add dry-run logic here
        
        # Start the engine
        engine.start()
        
        # Main monitoring loop
        logger.info(" Engine started, entering monitoring loop")
        
        while engine.running:
            try:
                current_time = datetime.now(pytz.timezone('Asia/Kolkata')).time()
                
                # Check for scheduled shutdown at 15:32
                if current_time >= time(15, 32):
                    logger.info("Scheduled shutdown time reached (15:32 IST)")
                    break

                status = engine.get_status()
                
                # Log status every 1 minute
                if datetime.now().minute % 1 == 0 and datetime.now().second < 5:
                    logger.info(f" STATUS: Positions: {status['open_positions']} | "
                               f"Daily Trades: {status['daily_trades']} | "
                               f"Market: {'OPEN' if status['market_hours'] else 'CLOSED'} | "
                               f"Trading: {'ACTIVE' if status['trading_hours'] else 'INACTIVE'}")
                
                # Log trade history periodically
                if len(engine.position_manager.trade_history) > 0:
                    recent_trades = len([t for t in engine.position_manager.trade_history 
                                       if t['exit_time'].date() == datetime.now().date()])
                    if recent_trades > 0:
                        total_pnl = sum(t['gross_pnl'] for t in engine.position_manager.trade_history 
                                      if t['exit_time'].date() == datetime.now().date())
                        logger.info(f" Today's Performance: {recent_trades} trades, P&L: {total_pnl:.2f}")
                
                time_module.sleep(30)  # Status check every 30 seconds
                
            except KeyboardInterrupt:
                logger.info("Keyboard interrupt")
                break
            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}")
                time_module.sleep(10)
        
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        import traceback
        logger.error(traceback.format_exc())
    finally:
        if engine:
            logger.info("Initiating engine shutdown sequence...")
            engine.stop()
        logger.info("Live Trading Engine shutdown complete")

if __name__ == "__main__":
    main()
