(() => {
  const authUrl = document.querySelector('meta[name="neon-auth-url"]')?.content;
  const nativeFetch = window.fetch.bind(window);
  let neonAuthPromise;
  let tokenPromise;

  window.runWhenAuthenticated = (callback) => {
    if (document.body.dataset.authState === "ready") {
      callback();
      return;
    }
    window.addEventListener("financas:authenticated", callback, { once: true });
  };

  function getNeonAuth() {
    if (!authUrl) return Promise.reject(new Error("URL do Neon Auth não configurada."));
    if (!neonAuthPromise) {
      neonAuthPromise = import("https://esm.sh/@neondatabase/neon-js@0.9.0-beta/auth?bundle")
        .then(({ createInternalNeonAuth }) => createInternalNeonAuth(authUrl));
    }
    return neonAuthPromise;
  }

  async function getToken() {
    if (!tokenPromise) {
      tokenPromise = (async () => (await getNeonAuth()).getJWTToken())();
    }
    const pending = tokenPromise;
    try {
      return await pending;
    } finally {
      if (tokenPromise === pending) tokenPromise = null;
    }
  }

  function redirectToLogin() {
    if (location.pathname === "/login" || location.pathname === "/cadastro") return;
    const next = `${location.pathname}${location.search}${location.hash}`;
    location.replace(`/login?next=${encodeURIComponent(next)}`);
  }

  function isApiRequest(input) {
    try {
      const requestUrl = input instanceof Request ? input.url : String(input);
      const parsed = new URL(requestUrl, location.href);
      return parsed.origin === location.origin && parsed.pathname.startsWith("/api/");
    } catch {
      return false;
    }
  }

  window.fetch = async (input, init = {}) => {
    if (!isApiRequest(input)) return nativeFetch(input, init);

    let token;
    try {
      token = await getToken();
    } catch {
      redirectToLogin();
      throw new Error("Não foi possível validar sua sessão.");
    }
    if (!token) {
      redirectToLogin();
      return new Response(JSON.stringify({ erro: "Autenticação necessária." }), {
        status: 401,
        headers: { "Content-Type": "application/json" },
      });
    }

    const headers = new Headers(input instanceof Request ? input.headers : undefined);
    new Headers(init.headers || {}).forEach((value, key) => headers.set(key, value));
    headers.set("Authorization", `Bearer ${token}`);
    const response = await nativeFetch(input, { ...init, headers });
    if (response.status === 401) redirectToLogin();
    return response;
  };

  function redirectAfterAuth() {
    const candidate = new URLSearchParams(location.search).get("next");
    let destination = "/";
    if (candidate) {
      try {
        const url = new URL(candidate, location.origin);
        if (url.origin === location.origin) {
          destination = `${url.pathname}${url.search}${url.hash}`;
        }
      } catch {
        destination = "/";
      }
    }
    location.replace(destination);
  }

  function showStatus(message, isError = true) {
    const status = document.getElementById("auth-status");
    if (!status) return;
    status.textContent = message;
    status.classList.toggle("is-error", isError);
    status.classList.toggle("is-success", !isError);
  }

  async function initializeAuthPage() {
    if (document.body.dataset.authPage !== "true") return;

    const form = document.getElementById("auth-form");
    const mode = document.body.dataset.authMode;
    const submit = document.getElementById("auth-submit");
    const switchLink = document.querySelector("[data-auth-switch]");
    if (!form || !submit) return;

    if (switchLink) {
      const next = new URLSearchParams(location.search).get("next");
      if (next) switchLink.href += `?next=${encodeURIComponent(next)}`;
    }

    try {
      if (await getToken()) {
        redirectAfterAuth();
        return;
      }
    } catch {
      showStatus("O serviço de autenticação está indisponível. Tente novamente.");
    }

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      showStatus("");

      const email = document.getElementById("auth-email").value.trim().toLowerCase();
      const password = document.getElementById("auth-password").value;
      const name = document.getElementById("auth-name")?.value.trim();
      const confirmation = document.getElementById("auth-confirm-password")?.value;

      if (mode === "register" && password !== confirmation) {
        showStatus("As senhas não coincidem.");
        return;
      }

      submit.disabled = true;
      submit.textContent = mode === "register" ? "Criando conta…" : "Entrando…";
      try {
        const neonAuth = await getNeonAuth();
        const result = mode === "register"
          ? await neonAuth.adapter.signUp.email({ name, email, password })
          : await neonAuth.adapter.signIn.email({ email, password });

        if (result?.error) {
          showStatus(mode === "register"
            ? "Não foi possível criar a conta com esses dados. Verifique as informações e tente novamente."
            : "E-mail ou senha inválidos. Confira os dados e tente novamente.");
          return;
        }

        if (await getToken()) {
          redirectAfterAuth();
          return;
        }

        showStatus("Conta criada. Confira seu e-mail para concluir a confirmação e depois entre.", false);
      } catch {
        showStatus("Não foi possível concluir a autenticação. Tente novamente.");
      } finally {
        submit.disabled = false;
        submit.textContent = mode === "register" ? "Criar conta" : "Entrar";
      }
    });
  }

  async function initializeProtectedPage() {
    if (document.body.dataset.authRequired !== "true") return;
    const loader = document.getElementById("auth-loader");

    try {
      const token = await getToken();
      if (!token) {
        redirectToLogin();
        return;
      }
      const response = await nativeFetch("/api/auth/me", {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok) {
        redirectToLogin();
        return;
      }

      const user = await response.json();
      const email = document.getElementById("auth-user-email");
      const avatar = document.getElementById("auth-avatar");
      if (email) email.textContent = user.email || "Conta";
      if (avatar) avatar.textContent = (user.email || "U").charAt(0).toUpperCase();
      document.body.dataset.authState = "ready";
      document.body.classList.remove("auth-pending");
      window.dispatchEvent(new Event("financas:authenticated"));
    } catch {
      if (loader) loader.textContent = "Não foi possível validar sua sessão. Atualize a página para tentar novamente.";
    }
  }

  function initializeSignOut() {
    document.querySelector("[data-auth-sign-out]")?.addEventListener("click", async () => {
      const button = document.querySelector("[data-auth-sign-out]");
      if (button) button.disabled = true;
      try {
        const neonAuth = await getNeonAuth();
        await neonAuth.adapter.signOut();
      } catch {
        // The local page still leaves protected content; the next request rechecks Neon Auth.
      }
      location.replace("/login");
    });
  }

  initializeSignOut();
  initializeAuthPage();
  initializeProtectedPage();
})();
