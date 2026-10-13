"""Free public-repository runner. Only verified matches create notifications."""
import base64
import json
import logging
import os
from pathlib import Path
import tempfile
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .inventory import BrowserInventory, InventoryError
from .state import State, idempotency_key


class GitHub:
    branch = 'monitor-state'
    file = 'monitor-state.sqlite3'

    def __init__(self, repo, token):
        self.repo, self.token, self.sha = repo, token, None

    def api(self, path, method='GET', data=None, missing_ok=False):
        request = Request('https://api.github.com/repos/' + self.repo + '/' + path,
                          data=None if data is None else json.dumps(data).encode(), method=method,
                          headers={'Authorization': 'Bearer ' + self.token,
                                   'Accept': 'application/vnd.github+json',
                                   'Content-Type': 'application/json', 'User-Agent': 'tesla-inventory-watcher'})
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as error:
            if missing_ok and error.code == 404:
                return None
            raise RuntimeError('GitHub HTTP %s' % error.code) from None

    def restore(self, path):
        ref = self.api('git/ref/heads/' + self.branch, missing_ok=True)
        if ref is None:
            main = self.api('git/ref/heads/main')
            self.api('git/refs', 'POST', {'ref': 'refs/heads/' + self.branch, 'sha': main['object']['sha']})
        result = self.api('contents/' + self.file + '?ref=' + self.branch, missing_ok=True)
        if result is not None:
            if result.get('encoding') != 'base64':
                raise RuntimeError('Unsupported stored state encoding')
            path.write_bytes(base64.b64decode(result['content']))
            self.sha = result['sha']

    def save(self, state, path):
        state.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        payload = {'message': 'Save inventory check state [skip ci]', 'branch': self.branch,
                   'content': base64.b64encode(path.read_bytes()).decode()}
        if self.sha:
            payload['sha'] = self.sha
        result = self.api('contents/' + self.file, 'PUT', payload)
        self.sha = result['content']['sha']

    def receipts(self):
        receipts = {}
        for page in range(1, 101):
            rows = self.api('issues?state=all&creator=github-actions%5Bbot%5D&per_page=100&page=%s' % page)
            if not isinstance(rows, list):
                raise RuntimeError('Could not read previous notifications')
            for issue in rows:
                body = issue.get('body') or ''
                if body.startswith('<!-- tesla-event:'):
                    marker = body.split('-->', 1)[0] + '-->'
                    receipts[marker] = str(issue['number'])
            if len(rows) < 100:
                return receipts
        raise RuntimeError('Notification history exceeds pagination limit')

    def notify(self, event, receipts):
        if not event['vin']:
            raise RuntimeError('Only vehicle matches may create notifications')
        marker = '<!-- tesla-event:%s -->' % idempotency_key(event['id'])
        if marker in receipts:
            return receipts[marker]
        owner = self.repo.split('/')[0]
        issue = self.api('issues', 'POST', {'title': event['subject'],
                         'body': marker + '\n@' + owner + '\n\n' + event['body'], 'assignees': [owner]})
        receipts[marker] = str(issue['number'])
        return receipts[marker]


def run(api, path, source_factory=BrowserInventory):
    api.restore(path)
    state = State(path)
    try:
        try:
            with source_factory() as source:
                matches = source.scan()
        except Exception as error:
            reason = str(error) if isinstance(error, InventoryError) else type(error).__name__
            state.failure('Inventory unknown: ' + reason)
            api.save(state, path)
            logging.warning('Inventory unavailable: %s; no notifications created', reason)
            return False
        state.success(matches)
        # Persist availability episodes BEFORE posting, so crash recovery reuses their IDs.
        api.save(state, path)
        pending = state.pending()
        if pending:
            receipts = api.receipts()
            for event in pending:
                receipt = api.notify(event, receipts)
                state.delivered(event['id'], receipt)
                api.save(state, path)
        logging.info('Complete check: %s matches', len(matches))
        return True
    finally:
        state.db.close()


def main():
    logging.basicConfig(level=logging.INFO)
    # Never silently consume billable private-runner time or send startup/test alerts.
    if os.environ.get('REPOSITORY_VISIBILITY') != 'public':
        raise RuntimeError('This free-only runner requires a public repository')
    with tempfile.TemporaryDirectory() as directory:
        try:
            run(GitHub(os.environ['GITHUB_REPOSITORY'], os.environ['GITHUB_TOKEN']),
                Path(directory) / 'state.sqlite3')
        except Exception as error:
            # Keep operational errors in logs without automatic workflow-failure mail.
            logging.error('Monitor incomplete (%s); no empty/error alert sent', type(error).__name__)


if __name__ == '__main__':
    main()
