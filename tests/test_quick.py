"""Quick test of date-leak behavior."""
import os, sys, tempfile, shutil, subprocess

# Add necropsy to path BEFORE any imports
necropsy_dir = os.path.join(os.path.dirname(__file__), "..", "necropsy")
if necropsy_dir not in sys.path:
    sys.path.insert(0, necropsy_dir)

from necropsy.db import init_db, add_repo
from necropsy.ingest.git import extract_commits
from necropsy.index.retrieve import retrieve


def main():
    # Create fixture repo
    tmpdir = tempfile.mkdtemp(prefix='necropsy_test_')
    try:
        subprocess.run(['git', 'init'], cwd=tmpdir, capture_output=True)
        subprocess.run(['git', 'config', 'user.name', 'Test'], cwd=tmpdir, capture_output=True)
        subprocess.run(['git', 'config', 'user.email', 'test@test.com'], cwd=tmpdir, capture_output=True)

        # Commit A: Jan 15
        with open(os.path.join(tmpdir, 'f.txt'), 'w') as f:
            f.write('a')
        subprocess.run(['git', 'add', 'f.txt'], cwd=tmpdir, capture_output=True)
        subprocess.run(['git', 'commit', '--allow-empty', '-m', 'Jan'], cwd=tmpdir, capture_output=True,
            env={**os.environ, 'GIT_AUTHOR_DATE': '2024-01-15T10:00:00Z', 'GIT_COMMITTER_DATE': '2024-01-15T10:00:00Z'})

        # Commit B: Feb 10
        subprocess.run(['git', 'commit', '--allow-empty', '-m', 'Feb'], cwd=tmpdir, capture_output=True,
            env={**os.environ, 'GIT_AUTHOR_DATE': '2024-02-10T10:00:00Z', 'GIT_COMMITTER_DATE': '2024-02-10T10:00:00Z'})

        # Commit C: Mar 20
        subprocess.run(['git', 'commit', '--allow-empty', '-m', 'Mar'], cwd=tmpdir, capture_output=True,
            env={**os.environ, 'GIT_AUTHOR_DATE': '2024-03-20T10:00:00Z', 'GIT_COMMITTER_DATE': '2024-03-20T10:00:00Z'})

        # Init DB and add repo
        init_db()
        repo_id = add_repo(source=tmpdir, name='test', status='pending')
        commits = extract_commits(tmpdir)
        added = add_commits(repo_id, commits)
        print(f'Added {added} commits')

        # Test retrieval with as_of=2024-02-01 (should only get Jan commit)
        results = retrieve(repo_id, 'test', '2024-02-01')
        dates = [r['at'] for r in results]
        print(f'Retrieval as_of=2024-02-01: {dates}')
        for r in results:
            assert r['at'] <= '2024-02-01T00:00:00Z', f'Date leak: {r["at"]}'
        print('PASS: No dates after as_of for Feb 1 cutoff')

        # Test retrieval with as_of=2024-02-15 (should get Jan and Feb)
        results2 = retrieve(repo_id, 'test', '2024-02-15')
        dates2 = [r['at'] for r in results2]
        print(f'Retrieval as_of=2024-02-15: {dates2}')
        for r in results2:
            assert r['at'] <= '2024-02-15T00:00:00Z', f'Date leak: {r["at"]}'
        print('PASS: No dates after as_of for Feb 15 cutoff')

        # Test retrieval with as_of=2024-03-31 (should get all three)
        results3 = retrieve(repo_id, 'test', '2024-03-31')
        dates3 = [r['at'] for r in results3]
        print(f'Retrieval as_of=2024-03-31: {dates3}')
        assert len(results3) == 3, f'Expected 3 results, got {len(results3)}'
        print('PASS: All 3 commits retrieved for Mar 31 cutoff')

        print('\nAll date-leak tests PASSED!')
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == '__main__':
    main()