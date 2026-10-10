"""Upload anime card art to Cloudflare R2 (S3-compatible), resumable.

Reads creds ONLY from env (never paste keys in chat or files):
  R2_ACCOUNT_ID, R2_ACCESS_KEY, R2_SECRET_KEY, R2_BUCKET (default babka-cards)

Only the 8 chosen series go up (prefix rules in SERIES_PREFIXES).
Keys keep the original filename under cards/ — '#' stays literal in the key;
callers percent-encode it when building public URLs.

  py tools/upload_cards_r2.py [--dry-run] [--workers 8]

pip install boto3
"""
import os
import sys
from concurrent.futures import ThreadPoolExecutor

SERIES_PREFIXES = ('naruto_', 'boruto_', 're_zero_', 'bleach_', 'dragon_ball_',
                   'one_piece_', 'demon_slayer_', 'jojo_', 'blue_lock_', 'fairy_tail_')
SRC = r'F:\Files\Documents\!! Projects\babka cards'
KEY_PREFIX = 'cards/'


def _client():
    import boto3
    acct = os.environ['R2_ACCOUNT_ID']
    return boto3.client(
        's3',
        endpoint_url=f'https://{acct}.r2.cloudflarestorage.com',
        aws_access_key_id=os.environ['R2_ACCESS_KEY'],
        aws_secret_access_key=os.environ['R2_SECRET_KEY'],
    )


def _files():
    out = []
    with os.scandir(SRC) as it:
        for e in it:
            if not e.name.lower().endswith('.png'):
                continue
            if e.name.lower().startswith(SERIES_PREFIXES):
                out.append(e.path)
    return sorted(out)


def main():
    dry = '--dry-run' in sys.argv
    workers = 8
    if '--workers' in sys.argv:
        try:
            workers = int(sys.argv[sys.argv.index('--workers') + 1])
        except Exception:
            pass
    bucket = os.environ.get('R2_BUCKET', 'babka-cards')
    files = _files()
    print(f'{len(files)} files queued for s3://{bucket}/{KEY_PREFIX}')
    if dry:
        print('dry run — nothing uploaded')
        return
    s3 = _client()

    # Fog/size cache: skip what is already up with the same byte count.
    def _remote_size(key):
        try:
            return s3.head_object(Bucket=bucket, Key=key)['ContentLength']
        except Exception:
            return -1

    def _put(path):
        name = os.path.basename(path)
        key = KEY_PREFIX + name
        try:
            if os.path.getsize(path) == _remote_size(key):
                return 'skip'
            s3.upload_file(path, bucket, key,
                           ExtraArgs={'ContentType': 'image/png',
                                      'CacheControl': 'public, max-age=31536000, immutable'})
            return 'up'
        except Exception as e:
            print(f'FAIL {name}: {type(e).__name__}: {e}')
            return 'fail'

    up = skip = fail = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i, res in enumerate(ex.map(_put, files), 1):
            if res == 'up':
                up += 1
            elif res == 'skip':
                skip += 1
            else:
                fail += 1
            if i % 250 == 0 or i == len(files):
                print(f'  {i}/{len(files)}  up={up} skip={skip} fail={fail}', flush=True)
    print(f'DONE up={up} skip={skip} fail={fail}')


if __name__ == '__main__':
    for v in ('R2_ACCOUNT_ID', 'R2_ACCESS_KEY', 'R2_SECRET_KEY'):
        if not os.environ.get(v):
            sys.exit(f'missing env {v} — set it in PowerShell, never in files')
    main()
