"""CLI: serve / refresh / digest / index-images / demo."""
import argparse
import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv


def main():
    load_dotenv(Path(__file__).resolve().parent.parent / '.env')
    parser = argparse.ArgumentParser(description='Freshhead personal wardrobe radar')
    parser.add_argument('command', nargs='?', default='serve', choices=['serve', 'refresh', 'digest', 'index-images', 'demo'])
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--store', default='')
    parser.add_argument('--send', action='store_true')
    parser.add_argument('--limit', type=int, default=200)
    args = parser.parse_args()
    from .db import DB
    db = DB()
    if args.command == 'serve':
        import uvicorn
        from .app import create_app
        uvicorn.run(create_app(db), host='127.0.0.1', port=args.port, log_level='warning')
    elif args.command == 'index-images':
        from .vision import index_images
        index_images(db, args.limit)
    elif args.command == 'demo':
        from .demo import seed_demo
        print(f'Loaded {seed_demo(db)} clearly labelled demo records.')
    else:
        from .service import Service
        from .catalog import STORES
        if args.store and args.store not in STORES:
            parser.error('Unknown store')
        service = Service(db)
        if args.command == 'refresh':
            asyncio.run(service.refresh(args.store or None))
            print(json.dumps(db.runs(), ensure_ascii=False, indent=2))
        else:
            result = asyncio.run(service.make_digest(send=args.send))
            print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
