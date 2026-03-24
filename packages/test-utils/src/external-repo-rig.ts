/**
 * @license
 * Copyright 2026 Google LLC
 * SPDX-License-Identifier: Apache-2.0
 */

import { execSync, spawn } from 'node:child_process';
import fs from 'node:fs';
import { join } from 'node:path';
import * as os from 'node:os';
import { env } from 'node:process';
import { sanitizeTestName } from './test-rig.js';

/**
 * The result of running an external repository's test suite.
 */
export interface ExternalRepoRunResult {
  /** Whether the test suite exited with code 0. */
  passed: boolean;
  /** Combined stdout + stderr from the test suite run. */
  output: string;
}

/**
 * A test rig for evaluating the Gemini CLI against real, externally-hosted
 * repositories. Unlike {@link TestRig}, which writes inline files to a temp
 * directory, ExternalRepoRig clones an actual repository at a pinned commit,
 * runs the CLI against it, applies the generated patch, and executes the
 * repo's own test suite as the evaluation oracle.
 *
 * Intended for use with the long-context coding evaluation dataset
 * (evals/datasets/long-context/).
 *
 * @example
 * ```typescript
 * const rig = new ExternalRepoRig();
 * rig.setup('my-task');
 * await rig.clone('https://github.com/org/repo', 'abc1234');
 * await rig.applyPatch(generatedPatch);
 * const result = await rig.runTestSuite('npm test', 900_000);
 * expect(result.passed).toBe(true);
 * rig.cleanup();
 * ```
 */
export class ExternalRepoRig {
  /** Absolute path to the cloned repository on disk. Null until setup() is called. */
  repoDir: string | null = null;

  private _testName?: string;

  /**
   * Initialises the rig: creates (or replaces) a clean working directory for
   * the repository clone.
   *
   * Must be called before any other method.
   */
  setup(testName: string): void {
    this._testName = testName;
    const sanitizedName = sanitizeTestName(testName);
    const baseDir =
      env['INTEGRATION_TEST_FILE_DIR'] || join(os.tmpdir(), 'gemini-lc-eval');
    this.repoDir = join(baseDir, sanitizedName);

    if (fs.existsSync(this.repoDir)) {
      fs.rmSync(this.repoDir, { recursive: true, force: true });
    }
    fs.mkdirSync(this.repoDir, { recursive: true });
  }

  /**
   * Clones the repository at the given URL and checks out the specified commit.
   *
   * Uses `--filter=blob:none` (partial clone) to avoid downloading the full
   * blob history, which significantly reduces clone time for large repos.
   *
   * @param repoUrl - HTTPS URL of the repository to clone.
   * @param baseCommit - The exact commit SHA to check out.
   */
  async clone(repoUrl: string, baseCommit: string): Promise<void> {
    if (!this.repoDir) {
      throw new Error('ExternalRepoRig.setup() must be called before clone().');
    }

    const stdio = env['VERBOSE'] === 'true' ? 'inherit' : 'pipe';

    execSync(`git clone --filter=blob:none ${repoUrl} .`, {
      cwd: this.repoDir,
      stdio,
    });

    execSync(`git checkout ${baseCommit}`, {
      cwd: this.repoDir,
      stdio,
    });
  }

  /**
   * Applies a unified diff patch to the cloned repository using `git apply`.
   *
   * The patch text is piped directly to stdin of `git apply -`, so no
   * temporary file is created.
   *
   * @param patchText - Unified diff string to apply.
   */
  async applyPatch(patchText: string): Promise<void> {
    if (!this.repoDir) {
      throw new Error(
        'ExternalRepoRig.setup() must be called before applyPatch().',
      );
    }

    execSync('git apply --whitespace=nowarn -', {
      cwd: this.repoDir,
      input: patchText,
      stdio: ['pipe', env['VERBOSE'] === 'true' ? 'inherit' : 'pipe', 'pipe'],
    });
  }

  /**
   * Executes the repository's test suite and returns a pass/fail result.
   *
   * The command is run with `shell: true` so that multi-command strings
   * (e.g. `npm install && npm test`) work without additional parsing.
   * stdout and stderr are merged into the returned `output` string.
   *
   * @param command - Shell command to run (e.g. `npm test`, `pytest -x`).
   * @param timeoutMs - Maximum time to wait before killing the process and
   *   returning a failure. Defaults to 15 minutes.
   */
  runTestSuite(
    command: string,
    timeoutMs = 900_000,
  ): Promise<ExternalRepoRunResult> {
    if (!this.repoDir) {
      throw new Error(
        'ExternalRepoRig.setup() must be called before runTestSuite().',
      );
    }

    return new Promise((resolve) => {
      const child = spawn(command, [], {
        cwd: this.repoDir!,
        stdio: 'pipe',
        shell: true,
      });

      let output = '';

      child.stdout.setEncoding('utf8');
      child.stdout.on('data', (data: string) => {
        output += data;
        if (env['VERBOSE'] === 'true') process.stdout.write(data);
      });

      child.stderr.setEncoding('utf8');
      child.stderr.on('data', (data: string) => {
        output += data;
        if (env['VERBOSE'] === 'true') process.stderr.write(data);
      });

      const timer = setTimeout(() => {
        child.kill('SIGKILL');
        resolve({
          passed: false,
          output: `Test suite timed out after ${timeoutMs}ms.\n${output}`,
        });
      }, timeoutMs);

      child.on('error', (err: Error) => {
        clearTimeout(timer);
        resolve({
          passed: false,
          output: `Failed to start test suite: ${err.message}\n${output}`,
        });
      });

      child.on('close', (code: number | null) => {
        clearTimeout(timer);
        resolve({ passed: code === 0, output });
      });
    });
  }

  /**
   * Removes the cloned repository directory and resets internal state.
   *
   * Respects the `KEEP_OUTPUT` environment variable: if set to `'true'`,
   * the directory is left on disk for post-mortem inspection.
   */
  cleanup(): void {
    if (this.repoDir && fs.existsSync(this.repoDir) && !env['KEEP_OUTPUT']) {
      try {
        fs.rmSync(this.repoDir, { recursive: true, force: true });
      } catch (error) {
        if (env['VERBOSE'] === 'true' || env['CI'] === 'true') {
          console.warn(
            `ExternalRepoRig cleanup warning (${this._testName}):`,
            (error as Error).message,
          );
        }
      }
    }
    this.repoDir = null;
  }
}
