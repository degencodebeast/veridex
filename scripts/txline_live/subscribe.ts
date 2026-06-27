/**
 * TxLINE devnet subscription script
 * Executes the on-chain `subscribe` instruction (World Cup FREE tier)
 * and activates the API token via the TxLINE activation endpoint.
 *
 * SECURITY: Never prints JWT or API token values to stdout.
 */

import * as anchor from "@coral-xyz/anchor";
import { AnchorProvider, Program, Wallet } from "@coral-xyz/anchor";
import {
  Connection,
  Keypair,
  PublicKey,
  SystemProgram,
  Transaction,
  sendAndConfirmTransaction,
} from "@solana/web3.js";
import {
  TOKEN_2022_PROGRAM_ID,
  ASSOCIATED_TOKEN_PROGRAM_ID,
  getAssociatedTokenAddressSync,
  createAssociatedTokenAccountInstruction,
  getAccount,
  TokenAccountNotFoundError,
  TokenInvalidAccountOwnerError,
} from "@solana/spl-token";
import * as nacl from "tweetnacl";
import * as fs from "fs";
import * as path from "path";

// ── Constants ────────────────────────────────────────────────────────────────
const PROGRAM_ID = new PublicKey("6pW64gN1s2uqjHkn1unFeEjAwJkPGHoppGvS715wyP2J");
const TOKEN_MINT = new PublicKey("4Zao8ocPhmMgq7PdsYWyxvqySMGx7xb9cMftPMkEokRG");
const RPC_URL   = "https://api.devnet.solana.com";

// Devnet-only spike: do NOT send a devnet txSig/JWT/wallet-signature to prod activation hosts.
// Pass --allow-prod-hosts to additionally try prod (rarely needed).
const DEVNET_ACTIVATE_HOSTS = [
  "https://oracle-dev.txodds.com/api/token/activate",
  "https://txline-dev.txodds.com/api/token/activate",
];
const PROD_ACTIVATE_HOSTS = [
  "https://oracle.txodds.com/api/token/activate",
  "https://txline.txodds.com/api/token/activate",
];
const ACTIVATE_HOSTS = process.argv.includes("--allow-prod-hosts")
  ? [...DEVNET_ACTIVATE_HOSTS, ...PROD_ACTIVATE_HOSTS]
  : DEVNET_ACTIVATE_HOSTS;

// ── Load wallet keypair ──────────────────────────────────────────────────────
const keypairPath = path.join(
  process.env.HOME!,
  ".config/solana/proofarena-treasury-devnet.json"
);
const walletKeypair = Keypair.fromSecretKey(
  Uint8Array.from(JSON.parse(fs.readFileSync(keypairPath, "utf-8")))
);

// ── Read JWT from veridex/.env (never printed) ───────────────────────────────
const envPath = path.resolve(__dirname, "../../veridex/.env");
const envContent = fs.readFileSync(envPath, "utf-8");
// Support quoted and unquoted values, and CRLF line endings.
// The file uses var name JWT (confirmed from file inspection).
const jwtMatch =
  envContent.match(/^TxLINE_API_TOKEN=["']?([^"'\r\n]+)["']?\r?$/m) ??
  envContent.match(/^JWT=["']?([^"'\r\n]+)["']?\r?$/m);
if (!jwtMatch) {
  // Show variable names (never values) for debugging
  const varNames = envContent.split(/\r?\n/)
    .filter(l => l.includes("=") && !l.startsWith("#"))
    .map(l => l.split("=")[0]);
  console.error("[DEBUG] Variables in .env:", varNames);
  throw new Error("Guest JWT not found in veridex/.env (tried TxLINE_API_TOKEN and JWT)");
}
const guestJwt = jwtMatch[1].trim();
console.log(`[INFO] JWT loaded (length=${guestJwt.length})`);

// ── Load IDL ─────────────────────────────────────────────────────────────────
const idl = JSON.parse(
  fs.readFileSync(path.join(__dirname, "txoracle.idl.json"), "utf-8")
);

async function main() {
  // ── Provider + program ─────────────────────────────────────────────────────
  const connection = new Connection(RPC_URL, "confirmed");
  const wallet     = new Wallet(walletKeypair);
  const provider   = new AnchorProvider(connection, wallet, {
    commitment: "confirmed",
    preflightCommitment: "confirmed",
  });
  anchor.setProvider(provider);

  const program = new Program(idl, provider) as any;

  // ── CLI: optional --tx-sig=<sig> to skip on-chain step ───────────────────
  const txSigArg = process.argv.find(a => a.startsWith("--tx-sig="))?.split("=")[1];
  if (txSigArg) {
    console.log(`[INFO] Using provided txSig (skipping on-chain): ${txSigArg}`);
  }

  // ── Balance check ──────────────────────────────────────────────────────────
  const lamports = await connection.getBalance(walletKeypair.publicKey);
  const sol = lamports / 1e9;
  console.log(`[INFO] Wallet: ${walletKeypair.publicKey.toBase58()}`);
  console.log(`[INFO] Balance: ${sol.toFixed(6)} SOL`);
  if (sol < 0.1) {
    throw new Error(`Insufficient balance: ${sol} SOL (need > 0.1)`);
  }

  // ── Derive PDAs ────────────────────────────────────────────────────────────
  const [pricingMatrix] = PublicKey.findProgramAddressSync(
    [Buffer.from("pricing_matrix")],
    PROGRAM_ID
  );
  const [tokenTreasuryPda] = PublicKey.findProgramAddressSync(
    [Buffer.from("token_treasury_v2")],
    PROGRAM_ID
  );
  console.log(`[INFO] pricing_matrix:     ${pricingMatrix.toBase58()}`);
  console.log(`[INFO] token_treasury_pda: ${tokenTreasuryPda.toBase58()}`);

  // ── Derive ATAs (TOKEN-2022) ────────────────────────────────────────────────
  const userTokenAccount = getAssociatedTokenAddressSync(
    TOKEN_MINT,
    walletKeypair.publicKey,
    false,                       // allowOwnerOffCurve = false for regular user
    TOKEN_2022_PROGRAM_ID,
    ASSOCIATED_TOKEN_PROGRAM_ID
  );
  const tokenTreasuryVault = getAssociatedTokenAddressSync(
    TOKEN_MINT,
    tokenTreasuryPda,
    true,                        // allowOwnerOffCurve = true (PDA owner)
    TOKEN_2022_PROGRAM_ID,
    ASSOCIATED_TOKEN_PROGRAM_ID
  );
  console.log(`[INFO] user_token_account:   ${userTokenAccount.toBase58()}`);
  console.log(`[INFO] token_treasury_vault: ${tokenTreasuryVault.toBase58()}`);

  let txSig: string;

  if (txSigArg) {
    // Skip on-chain work — use the already-confirmed tx
    txSig = txSigArg;
    console.log(`[INFO] Skipping on-chain steps; reusing txSig`);
  } else {
    // ── Ensure user ATA exists (create if missing) ───────────────────────────
    let ataExists = false;
    try {
      await getAccount(connection, userTokenAccount, "confirmed", TOKEN_2022_PROGRAM_ID);
      ataExists = true;
      console.log("[INFO] User TOKEN-2022 ATA already exists");
    } catch (e) {
      if (
        e instanceof TokenAccountNotFoundError ||
        e instanceof TokenInvalidAccountOwnerError
      ) {
        console.log("[INFO] User TOKEN-2022 ATA not found – creating...");
      } else {
        throw e;
      }
    }

    if (!ataExists) {
      const createAtaIx = createAssociatedTokenAccountInstruction(
        walletKeypair.publicKey,
        userTokenAccount,
        walletKeypair.publicKey,
        TOKEN_MINT,
        TOKEN_2022_PROGRAM_ID,
        ASSOCIATED_TOKEN_PROGRAM_ID
      );
      const ataTx = new Transaction().add(createAtaIx);
      const ataSig = await sendAndConfirmTransaction(connection, ataTx, [walletKeypair]);
      console.log(`[INFO] ATA created, sig: ${ataSig}`);
    }

    // ── Call subscribe(service_level_id=1, weeks=4) ──────────────────────────
    console.log("[INFO] Sending subscribe instruction...");
    try {
      txSig = await program.methods
        .subscribe(1, 4)
        .accounts({
          user:                   walletKeypair.publicKey,
          pricingMatrix:          pricingMatrix,
          tokenMint:              TOKEN_MINT,
          userTokenAccount:       userTokenAccount,
          tokenTreasuryVault:     tokenTreasuryVault,
          tokenTreasuryPda:       tokenTreasuryPda,
          tokenProgram:           TOKEN_2022_PROGRAM_ID,
          systemProgram:          SystemProgram.programId,
          associatedTokenProgram: ASSOCIATED_TOKEN_PROGRAM_ID,
        })
        .rpc();
    } catch (e: any) {
      console.error("[ERROR] subscribe instruction failed:", e.message ?? e);
      if (e.logs && Array.isArray(e.logs)) {
        console.error("[LOGS]");
        e.logs.forEach((l: string) => console.error("  ", l));
      }
      throw e;
    }
    console.log(`[INFO] subscribe txSig: ${txSig}`);
  }

  // ── Build activation message and sign ──────────────────────────────────────
  // Message format: "{txSig}::{jwt}"  (empty leagues → empty leagues_csv)
  const message      = `${txSig}::${guestJwt}`;
  const messageBytes = Buffer.from(message, "utf-8");
  const sig64        = nacl.sign.detached(messageBytes, walletKeypair.secretKey);
  const walletSignature = Buffer.from(sig64).toString("base64");
  // Do NOT log walletSignature (it encodes the JWT indirectly via the message)

  // ── Helper: get a fresh guest JWT (needed when existing JWT is IP-bound) ──
  async function getFreshJwt(host: string): Promise<string | null> {
    const base = host.replace("/api/token/activate", "");
    const url = `${base}/auth/guest/start`;
    try {
      const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" } });
      if (r.status === 200) {
        const j = await r.json() as any;
        if (j?.token && typeof j.token === "string") {
          console.log(`[INFO] Fresh JWT obtained from ${url} (length=${j.token.length})`);
          return j.token as string;
        }
      }
      console.log(`[WARN] guest/start at ${url} returned ${r.status}`);
    } catch (e: any) {
      console.log(`[WARN] guest/start at ${url} failed: ${e.message}`);
    }
    return null;
  }

  // ── POST to activation endpoints ───────────────────────────────────────────
  let apiToken: string | null = null;
  let successHost: string | null = null;

  for (const host of ACTIVATE_HOSTS) {
    console.log(`[INFO] Trying activation host: ${host}`);

    // Try up to two JWTs: existing first, then a fresh one if 401
    const jwtCandidates: { label: string; jwt: string }[] = [
      { label: "existing", jwt: guestJwt },
    ];

    let activated = false;
    for (const { label, jwt } of jwtCandidates) {
      // Re-sign the message for this jwt (message includes the jwt itself)
      const msg   = `${txSig}::${jwt}`;
      const sig64 = nacl.sign.detached(Buffer.from(msg, "utf-8"), walletKeypair.secretKey);
      const wSig  = Buffer.from(sig64).toString("base64");

      const reqBody = JSON.stringify({ txSig, walletSignature: wSig, leagues: [] });
      try {
        const res = await fetch(host, {
          method: "POST",
          headers: { Authorization: `Bearer ${jwt}`, "Content-Type": "application/json" },
          body: reqBody,
        });
        console.log(`[INFO]   [${label} JWT] HTTP ${res.status}`);

        if (res.status === 200) {
          const text = (await res.text()).trim();
          if (text.startsWith("txoracle_api_")) {
            apiToken    = text;
            successHost = host;
            // If we used a fresh JWT, save it back to .env
            if (label !== "existing") {
              const newEnv = envContent.replace(/^JWT=.*$/m, `JWT=${jwt}`);
              if (newEnv !== envContent) {
                fs.writeFileSync(envPath, newEnv, "utf-8");
              } else {
                fs.appendFileSync(envPath, `\nJWT=${jwt}\n`, "utf-8");
              }
              console.log("[INFO] Fresh JWT saved to veridex/.env (JWT=)");
            }
            console.log(`[INFO] Token received (length=${apiToken.length}, prefix=${apiToken.slice(0, 13)})`);
            activated = true;
            break;
          } else {
            console.log(`[WARN]   Unexpected body prefix: "${text.slice(0, 60)}"`);
          }
        } else if (res.status === 401 && label === "existing") {
          const errBody = (await res.text()).trim();
          console.log(`[WARN]   401 with ${label} JWT (likely IP-bound) — fetching fresh JWT...`);
          if (errBody) console.log(`[WARN]   body: "${errBody.slice(0, 100)}"`);
          // Get a fresh JWT and push it as the next candidate
          const fresh = await getFreshJwt(host);
          if (fresh) {
            jwtCandidates.push({ label: "fresh", jwt: fresh });
          } else {
            console.log(`[WARN]   Could not obtain fresh JWT from this host`);
          }
        } else {
          const errBody = (await res.text()).trim();
          console.log(`[WARN]   Error body: "${errBody.slice(0, 200)}"`);
        }
      } catch (e: any) {
        console.log(`[WARN]   Request failed: ${e.message}`);
      }
    }

    if (activated) break;
  }

  if (!apiToken) {
    throw new Error("All activation hosts failed – see warnings above");
  }

  // ── Append TXLINE_X_API_TOKEN to veridex/.env ──────────────────────────────
  // Ensure we don't already have a TXLINE_X_API_TOKEN line
  let updatedEnv = envContent;
  if (/^TXLINE_X_API_TOKEN=/m.test(updatedEnv)) {
    // Replace existing line
    updatedEnv = updatedEnv.replace(/^TXLINE_X_API_TOKEN=.*$/m, `TXLINE_X_API_TOKEN=${apiToken}`);
    fs.writeFileSync(envPath, updatedEnv, "utf-8");
    console.log("[INFO] TXLINE_X_API_TOKEN line updated in veridex/.env");
  } else {
    // Append new line
    const suffix = updatedEnv.endsWith("\n") ? "" : "\n";
    fs.appendFileSync(envPath, `${suffix}TXLINE_X_API_TOKEN=${apiToken}\n`, "utf-8");
    console.log("[INFO] TXLINE_X_API_TOKEN appended to veridex/.env");
  }

  // ── Final summary (no secret values) ──────────────────────────────────────
  console.log("\n=== DONE ===");
  console.log(`txSig:        ${txSig}`);
  console.log(`host:         ${successHost}`);
  console.log(`token written: YES`);
  console.log(`token length:  ${apiToken.length}`);
  console.log(`token prefix:  ${apiToken.slice(0, 13)}`);
}

main().catch((e) => {
  console.error("[FATAL]", e);
  process.exit(1);
});
