// 解密门禁：与 pipeline/publish.py 同算法（M38 认证加密版）
// PBKDF2-HMAC-SHA256(口令, salt16, 200000, 64B) → 前32B 加密密钥 / 后32B MAC 密钥
// 格式：salt(16) + ct + tag(32)。先验 tag（encrypt-then-MAC），再 XOR 解流。
// 注意：IIFE 必须是同步的——async IIFE 会把 Promise 赋给 window.AStocker。
window.AStocker = (function () {
  async function deriveKeys(password, salt) {
    const enc = new TextEncoder();
    const base = await crypto.subtle.importKey(
      "raw", enc.encode(password), "PBKDF2", false, ["deriveBits"]);
    const bits = new Uint8Array(await crypto.subtle.deriveBits(
      { name: "PBKDF2", hash: "SHA-256", salt, iterations: 200000 },
      base, 512));
    return { encKey: bits.slice(0, 32), macKey: bits.slice(32, 64) };
  }

  async function keystream(encKeyRaw, n) {
    // WebCrypto HMAC importKey 必须显式指定 hash（与 Python hmac.new(…, sha256) 对齐）
    const key = await crypto.subtle.importKey(
      "raw", encKeyRaw, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
    const out = new Uint8Array(n);
    let off = 0, counter = 0;
    while (off < n) {
      const ctr = new DataView(new ArrayBuffer(4));
      ctr.setUint32(0, counter++);
      const mac = new Uint8Array(
        await crypto.subtle.sign("HMAC", key, ctr.buffer));
      for (let i = 0; i < mac.length && off < n; i++) out[off++] = mac[i];
    }
    return out;
  }

  async function decrypt(blob, password) {
    const salt = new Uint8Array(blob.slice(0, 16));
    const ct = new Uint8Array(blob.slice(16, blob.byteLength - 32));
    const tag = new Uint8Array(blob.slice(blob.byteLength - 32));
    const { encKey, macKey } = await deriveKeys(password, salt);
    // 完整性校验：HMAC-SHA256(macKey, salt+ct) === tag
    const macKeyImp = await crypto.subtle.importKey(
      "raw", macKey, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
    const payload = new Uint8Array(salt.length + ct.length);
    payload.set(salt); payload.set(ct, salt.length);
    const want = new Uint8Array(
      await crypto.subtle.sign("HMAC", macKeyImp, payload));
    if (want.length !== tag.length) throw new Error("tag mismatch");
    let diff = 0;
    for (let i = 0; i < tag.length; i++) diff |= want[i] ^ tag[i];
    if (diff !== 0) throw new Error("口令错误或密文被篡改");
    const ks = await keystream(encKey, ct.length);
    const pt = new Uint8Array(ct.length);
    for (let i = 0; i < ct.length; i++) pt[i] = ct[i] ^ ks[i];
    return JSON.parse(new TextDecoder().decode(pt));
  }

  async function open(password) {
    const res = await fetch("data/owner.bin");
    if (!res.ok) throw new Error("数据包未部署");
    return decrypt(await res.arrayBuffer(), password);
  }

  return { open, decrypt };
})();
