/**
 * @license
 * Copyright 2026 Google LLC
 * SPDX-License-Identifier: Apache-2.0
 */

import {
  describe,
  it,
  expect,
  vi,
  beforeEach,
  afterEach,
  type MockInstance,
} from 'vitest';
import { execSync, spawn, type ChildProcess } from 'node:child_process';
import fs from 'node:fs';
import { ExternalRepoRig } from './external-repo-rig.js';

// ---------------------------------------------------------------------------
// Module mocks
// ---------------------------------------------------------------------------

vi.mock('node:child_process', async (importOriginal) => {
  const actual = await importOriginal();
  return {
    ...(actual as object),
    execSync: vi.fn(),
    spawn: vi.fn(),
  };
});

vi.mock('node:fs', async (importOriginal) => {
  const actual = await importOriginal();
  return {
    ...(actual as object),
    default: {
      ...(actual as { default: object }).default,
      existsSync: vi.fn(),
      mkdirSync: vi.fn(),
      rmSync: vi.fn(),
    },
  };
});

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Creates a minimal mock child process with controllable stdout/stderr/events. */
function makeMockChild() {
  const listeners: Record<string, ((...args: unknown[]) => void)[]> = {};

  const on = vi.fn((event: string, cb: (...args: unknown[]) => void) => {
    if (!listeners[event]) listeners[event] = [];
    listeners[event].push(cb);
  });

  const emit = (event: string, ...args: unknown[]) => {
    (listeners[event] ?? []).forEach((cb) => cb(...args));
  };

  const stdStream = () => {
    const streamListeners: Record<string, ((...args: unknown[]) => void)[]> =
      {};
    return {
      setEncoding: vi.fn(),
      on: vi.fn((event: string, cb: (...args: unknown[]) => void) => {
        if (!streamListeners[event]) streamListeners[event] = [];
        streamListeners[event].push(cb);
      }),
      _emit: (event: string, ...args: unknown[]) =>
        (streamListeners[event] ?? []).forEach((cb) => cb(...args)),
    };
  };

  const stdout = stdStream();
  const stderr = stdStream();

  return {
    stdout,
    stderr,
    on,
    kill: vi.fn(),
    _emit: emit,
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('ExternalRepoRig', () => {
  let rig: ExternalRepoRig;
  let mockExecSync: MockInstance;
  let mockSpawn: MockInstance;
  let mockFs: typeof fs;

  beforeEach(() => {
    vi.resetAllMocks();
    rig = new ExternalRepoRig();

    mockExecSync = vi.mocked(execSync);
    mockSpawn = vi.mocked(spawn);
    mockFs = vi.mocked(fs);

    // Default: directory does not exist before setup
    (mockFs.existsSync as unknown as MockInstance).mockReturnValue(false);
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  // -------------------------------------------------------------------------
  // setup()
  // -------------------------------------------------------------------------

  describe('setup()', () => {
    it('creates the repo directory', () => {
      rig.setup('my-eval-task');

      expect(mockFs.mkdirSync).toHaveBeenCalledWith(
        expect.stringContaining('my-eval-task'),
        { recursive: true },
      );
      expect(rig.repoDir).not.toBeNull();
    });

    it('removes an existing directory before creating a fresh one', () => {
      (mockFs.existsSync as unknown as MockInstance).mockReturnValue(true);

      rig.setup('existing-task');

      expect(mockFs.rmSync).toHaveBeenCalledWith(
        expect.stringContaining('existing-task'),
        { recursive: true, force: true },
      );
      expect(mockFs.mkdirSync).toHaveBeenCalled();
    });

    it('sanitizes the test name for use as a directory name', () => {
      rig.setup('My Task With Spaces & Special!Chars');

      expect(rig.repoDir).toMatch(/my-task-with-spaces-special-chars/);
    });
  });

  // -------------------------------------------------------------------------
  // clone()
  // -------------------------------------------------------------------------

  describe('clone()', () => {
    it('throws if setup() was not called first', async () => {
      await expect(
        rig.clone('https://github.com/org/repo', 'abc1234'),
      ).rejects.toThrow('setup()');
    });

    it('runs git clone then git checkout', async () => {
      rig.setup('clone-test');

      await rig.clone('https://github.com/org/repo', 'deadbeef');

      expect(mockExecSync).toHaveBeenCalledTimes(2);
      expect(mockExecSync).toHaveBeenNthCalledWith(
        1,
        expect.stringContaining('git clone'),
        expect.objectContaining({ cwd: rig.repoDir }),
      );
      expect(mockExecSync).toHaveBeenNthCalledWith(
        2,
        'git checkout deadbeef',
        expect.objectContaining({ cwd: rig.repoDir }),
      );
    });

    it('uses --filter=blob:none for a partial clone', async () => {
      rig.setup('partial-clone-test');

      await rig.clone('https://github.com/org/repo', 'abc1234');

      expect(mockExecSync).toHaveBeenNthCalledWith(
        1,
        expect.stringContaining('--filter=blob:none'),
        expect.anything(),
      );
    });
  });

  // -------------------------------------------------------------------------
  // applyPatch()
  // -------------------------------------------------------------------------

  describe('applyPatch()', () => {
    it('throws if setup() was not called first', async () => {
      await expect(rig.applyPatch('--- a/file.ts')).rejects.toThrow('setup()');
    });

    it('pipes the patch text to git apply via stdin', async () => {
      rig.setup('patch-test');
      const patch = '--- a/file.ts\n+++ b/file.ts\n@@ -1 +1 @@\n-old\n+new\n';

      await rig.applyPatch(patch);

      expect(mockExecSync).toHaveBeenCalledWith(
        'git apply --whitespace=nowarn -',
        expect.objectContaining({
          cwd: rig.repoDir,
          input: patch,
        }),
      );
    });
  });

  // -------------------------------------------------------------------------
  // runTestSuite()
  // -------------------------------------------------------------------------

  describe('runTestSuite()', () => {
    it('throws if setup() was not called first', () => {
      expect(() => rig.runTestSuite('npm test')).toThrow('setup()');
    });

    it('returns passed:true when the process exits with code 0', async () => {
      rig.setup('passing-tests');
      const mockChild = makeMockChild();
      mockSpawn.mockReturnValue(mockChild as unknown as ChildProcess);

      const resultPromise = rig.runTestSuite('npm test');

      mockChild.stdout._emit('data', 'All tests passed\n');
      mockChild._emit('close', 0);

      const result = await resultPromise;
      expect(result.passed).toBe(true);
      expect(result.output).toContain('All tests passed');
    });

    it('returns passed:false when the process exits with non-zero code', async () => {
      rig.setup('failing-tests');
      const mockChild = makeMockChild();
      mockSpawn.mockReturnValue(mockChild as unknown as ChildProcess);

      const resultPromise = rig.runTestSuite('npm test');

      mockChild.stderr._emit('data', '3 tests failed\n');
      mockChild._emit('close', 1);

      const result = await resultPromise;
      expect(result.passed).toBe(false);
      expect(result.output).toContain('3 tests failed');
    });

    it('returns passed:false and kills the process on timeout', async () => {
      vi.useFakeTimers();
      rig.setup('timeout-test');
      const mockChild = makeMockChild();
      mockSpawn.mockReturnValue(mockChild as unknown as ChildProcess);

      const resultPromise = rig.runTestSuite('npm test', 5_000);

      vi.advanceTimersByTime(5_001);
      const result = await resultPromise;

      expect(result.passed).toBe(false);
      expect(result.output).toContain('timed out after 5000ms');
      expect(mockChild.kill).toHaveBeenCalledWith('SIGKILL');

      vi.useRealTimers();
    });

    it('returns passed:false when the process fails to start', async () => {
      rig.setup('error-test');
      const mockChild = makeMockChild();
      mockSpawn.mockReturnValue(mockChild as unknown as ChildProcess);

      const resultPromise = rig.runTestSuite('nonexistent-command');

      mockChild._emit('error', new Error('spawn nonexistent-command ENOENT'));
      const result = await resultPromise;

      expect(result.passed).toBe(false);
      expect(result.output).toContain('ENOENT');
    });

    it('merges stdout and stderr into the output string', async () => {
      rig.setup('merged-output-test');
      const mockChild = makeMockChild();
      mockSpawn.mockReturnValue(mockChild as unknown as ChildProcess);

      const resultPromise = rig.runTestSuite('npm test');

      mockChild.stdout._emit('data', 'stdout line\n');
      mockChild.stderr._emit('data', 'stderr line\n');
      mockChild._emit('close', 0);

      const result = await resultPromise;
      expect(result.output).toContain('stdout line');
      expect(result.output).toContain('stderr line');
    });

    it('runs the command with shell:true', () => {
      rig.setup('shell-test');
      const mockChild = makeMockChild();
      mockSpawn.mockReturnValue(mockChild as unknown as ChildProcess);

      rig.runTestSuite('npm install && npm test');

      expect(mockSpawn).toHaveBeenCalledWith(
        'npm install && npm test',
        [],
        expect.objectContaining({ shell: true }),
      );
    });
  });

  // -------------------------------------------------------------------------
  // cleanup()
  // -------------------------------------------------------------------------

  describe('cleanup()', () => {
    it('removes the repo directory and nulls repoDir', () => {
      rig.setup('cleanup-test');
      (mockFs.existsSync as unknown as MockInstance).mockReturnValue(true);

      rig.cleanup();

      expect(mockFs.rmSync).toHaveBeenCalledWith(
        expect.stringContaining('cleanup-test'),
        { recursive: true, force: true },
      );
      expect(rig.repoDir).toBeNull();
    });

    it('does not remove the directory when KEEP_OUTPUT is set', () => {
      vi.stubEnv('KEEP_OUTPUT', 'true');
      rig.setup('keep-output-test');
      (mockFs.existsSync as unknown as MockInstance).mockReturnValue(true);

      rig.cleanup();

      // rmSync should not have been called for the repo dir
      // (it may have been called during setup for prior run cleanup, but repoDir
      // was null at that point so existsSync returned false there)
      const rmCalls = (mockFs.rmSync as unknown as MockInstance).mock.calls;
      const calledForRepoDir = rmCalls.some(
        (args: unknown[]) =>
          typeof args[0] === 'string' && args[0].includes('keep-output-test'),
      );
      // setup() saw existsSync=false so rmSync was not called; cleanup() should
      // also not call it because KEEP_OUTPUT is set.
      expect(calledForRepoDir).toBe(false);
    });

    it('is a no-op when repoDir is already null', () => {
      // Should not throw
      expect(() => rig.cleanup()).not.toThrow();
    });
  });
});
