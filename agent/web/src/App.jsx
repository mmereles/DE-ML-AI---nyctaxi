import { useRef, useState, useEffect } from "react";
import zones from "./zones.json";

// Fase 3: API de cotizacion directa (sin LLM, sin key de OpenAI - ya
// desplegada y validada). Fase 4.2: agente conversacional (BYOK, necesita
// la key del visitante). Dos backends distintos, por eso dos variables.
const QUOTE_API_URL = import.meta.env.VITE_QUOTE_API_URL || "";
const ASK_AGENT_URL = import.meta.env.VITE_ASK_AGENT_URL || "";

const EXAMPLE_QUESTIONS = [
  { label: "busiest pickup zone", text: "Which Manhattan zone had the most pickups overall?" },
  { label: "tomorrow's demand", text: "Where will demand be highest tomorrow at 8am?" },
];

const ZONES_BY_BOROUGH = zones.reduce((acc, z) => {
  (acc[z.borough] ||= []).push(z);
  return acc;
}, {});

function ZoneSelect({ id, label, value, onChange }) {
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      <select id={id} value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">Choose a zone…</option>
        {Object.entries(ZONES_BY_BOROUGH).map(([borough, list]) => (
          <optgroup key={borough} label={borough}>
            {list.map((z) => (
              <option key={z.id} value={z.id}>
                {z.zone}
              </option>
            ))}
          </optgroup>
        ))}
      </select>
    </div>
  );
}

function QuoteForm() {
  const [pickupZone, setPickupZone] = useState("");
  const [dropoffZone, setDropoffZone] = useState("");
  const [datetime, setDatetime] = useState("");
  const [passengers, setPassengers] = useState(1);
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e) {
    e.preventDefault();
    setError("");
    setResult(null);

    if (!pickupZone || !dropoffZone || !datetime) {
      setError("Fill in pickup, dropoff, and date/time.");
      return;
    }

    // datetime-local da "YYYY-MM-DDTHH:mm" - quote_api espera "YYYY-MM-DD HH:MM:SS"
    const pickup_datetime = datetime.replace("T", " ") + ":00";

    setLoading(true);
    try {
      const resp = await fetch(QUOTE_API_URL, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          pickup_zone: Number(pickupZone),
          dropoff_zone: Number(dropoffZone),
          pickup_datetime,
          passenger_count: Number(passengers),
        }),
      });
      const data = await resp.json();
      if (!resp.ok) {
        setError(data.error || `Error ${resp.status}`);
      } else {
        setResult(data);
      }
    } catch (err) {
      setError(`Couldn't reach the API: ${err.message}`);
    } finally {
      setLoading(false);
    }
  }

  return (
    <section className="card quote-card">
      <h2>Get a fare quote</h2>
      <p className="card-sub">Straight to the fare model (Phase 3) — no OpenAI key, no cost.</p>

      <form onSubmit={handleSubmit}>
        <div className="field-row">
          <ZoneSelect id="pickup" label="Pickup" value={pickupZone} onChange={setPickupZone} />
          <ZoneSelect id="dropoff" label="Dropoff" value={dropoffZone} onChange={setDropoffZone} />
        </div>
        <div className="field-row">
          <div className="field">
            <label htmlFor="datetime">Date & time</label>
            <input
              id="datetime"
              type="datetime-local"
              value={datetime}
              onChange={(e) => setDatetime(e.target.value)}
            />
          </div>
          <div className="field field-narrow">
            <label htmlFor="passengers">Passengers</label>
            <input
              id="passengers"
              type="number"
              min="1"
              max="6"
              value={passengers}
              onChange={(e) => setPassengers(e.target.value)}
            />
          </div>
        </div>
        <button type="submit" className="btn-primary" disabled={loading}>
          {loading ? "Getting quote…" : "Get quote"}
        </button>
      </form>

      {error && <p className="quote-error">{error}</p>}
      {result && (
        <div className="quote-result">
          <span className="quote-amount">${result.estimated_fare_total}</span>
          <span className="quote-currency">{result.currency}</span>
          <p className="quote-disclaimer">{result.disclaimer}</p>
        </div>
      )}
    </section>
  );
}

function ChatMessage({ role, text }) {
  return <div className={`msg ${role}`}>{text}</div>;
}

function ChatSection() {
  const [open, setOpen] = useState(false);
  const [apiKey, setApiKey] = useState("");
  const [question, setQuestion] = useState("");
  const [messages, setMessages] = useState([]);
  const [loading, setLoading] = useState(false);
  const chatEndRef = useRef(null);

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  async function handleSubmit(e) {
    e.preventDefault();
    const trimmedQuestion = question.trim();
    const trimmedKey = apiKey.trim();

    if (!trimmedQuestion) return;
    if (!trimmedKey) {
      setMessages((prev) => [
        ...prev,
        { role: "error", text: "Missing your OpenAI API key (field above) to ask a question." },
      ]);
      return;
    }

    setMessages((prev) => [...prev, { role: "user", text: trimmedQuestion }]);
    setQuestion("");
    setLoading(true);

    try {
      const resp = await fetch(ASK_AGENT_URL, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ question: trimmedQuestion, openai_api_key: trimmedKey }),
      });
      const data = await resp.json();

      if (!resp.ok) {
        setMessages((prev) => [...prev, { role: "error", text: data.error || `Error ${resp.status}` }]);
      } else {
        setMessages((prev) => [...prev, { role: "agent", text: data.answer }]);
      }
    } catch (err) {
      setMessages((prev) => [...prev, { role: "error", text: `Couldn't reach the agent: ${err.message}` }]);
    } finally {
      setLoading(false);
    }
  }

  return (
    <section className="card chat-card">
      <button type="button" className="chat-toggle" onClick={() => setOpen((v) => !v)}>
        <span>Ask the agent something else</span>
        <span className="chevron">{open ? "▾" : "▸"}</span>
      </button>

      {open && (
        <div className="chat-body">
          <p className="card-sub">
            For questions that aren't a direct quote (rankings, future demand, history). Uses your own
            OpenAI key — never stored.
          </p>

          <div className="field">
            <label htmlFor="apiKey">Your OpenAI API key</label>
            <input
              id="apiKey"
              type="password"
              placeholder="sk-..."
              autoComplete="off"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
            />
          </div>

          <div id="chat">
            {messages.map((m, i) => (
              <ChatMessage key={i} role={m.role} text={m.text} />
            ))}
            {loading && <div className="msg agent pending">Thinking (can take up to a minute)…</div>}
            <div ref={chatEndRef} />
          </div>

          <form className="ask" onSubmit={handleSubmit}>
            <input
              type="text"
              placeholder="Which zone had the most trips?"
              autoComplete="off"
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
            />
            <button type="submit" className="btn-primary" disabled={loading}>
              Ask
            </button>
          </form>

          <div className="examples">
            {EXAMPLE_QUESTIONS.map((ex) => (
              <button type="button" key={ex.label} onClick={() => setQuestion(ex.text)}>
                {ex.label}
              </button>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}

export default function App() {
  return (
    <main>
      <header>
        <h1>NYC Taxi — Quotes &amp; Data</h1>
        <p className="sub">
          Get an instant quote from the fare model, or ask the agent about historical trips and future
          demand.
        </p>
      </header>

      <QuoteForm />
      <ChatSection />
    </main>
  );
}
