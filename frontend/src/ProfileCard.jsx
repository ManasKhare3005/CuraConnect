import { useState } from "react";
import { updateProfile, deleteAccount } from "./api";

function formatDateDisplay(value) {
  if (!value) return "";
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (m) return `${m[2]}/${m[3]}/${m[1]}`;
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleDateString("en-US");
}

export default function ProfileCard({ user, token, onUpdate, onDeleteAccount }) {
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(user.name || "");
  const [dob, setDob] = useState(user.dob || "");
  const [weightKg, setWeightKg] = useState(user.weight_kg ?? "");
  const [heightCm, setHeightCm] = useState(user.height_cm ?? "");
  const [address, setAddress] = useState(user.address || "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  function startEditing() {
    setName(user.name || "");
    setDob(user.dob || "");
    setWeightKg(user.weight_kg ?? "");
    setHeightCm(user.height_cm ?? "");
    setAddress(user.address || "");
    setError("");
    setEditing(true);
  }

  function cancel() {
    setEditing(false);
    setError("");
  }

  async function handleDelete() {
    if (!window.confirm("Are you sure you want to delete your account? All your data will be permanently removed.")) return;
    try {
      await deleteAccount(token);
      onDeleteAccount();
    } catch (err) {
      setError(err.message || "Failed to delete account.");
    }
  }

  async function save() {
    setSaving(true);
    setError("");
    try {
      const updated = await updateProfile(token, {
        name: name.trim() || null,
        dob: dob || null,
        weight_kg: weightKg !== "" ? parseFloat(weightKg) : null,
        height_cm: heightCm !== "" ? parseFloat(heightCm) : null,
        address: address.trim() || null,
      });
      onUpdate(updated);
      setEditing(false);
    } catch (err) {
      setError(err.message || "Failed to save.");
    } finally {
      setSaving(false);
    }
  }

  const initial = (user.name || "?")[0].toUpperCase();

  if (editing) {
    return (
      <div className="pc">
        <div className="pc-header">
          <div className="pc-avatar">{initial}</div>
          <div><div className="pc-name">{user.name}</div><div className="pc-email">{user.email}</div></div>
        </div>

        <div className="pc-form">
          <label className="pc-field">
            <span className="pc-field-label">Name</span>
            <input type="text" value={name} onChange={(e) => setName(e.target.value)} />
          </label>
          <label className="pc-field">
            <span className="pc-field-label">Date of Birth</span>
            <input type="date" value={dob} onChange={(e) => setDob(e.target.value)} max={new Date().toISOString().split("T")[0]} />
          </label>
          <div className="pc-row-2">
            <label className="pc-field">
              <span className="pc-field-label">Weight (kg)</span>
              <input type="number" value={weightKg} onChange={(e) => setWeightKg(e.target.value)} placeholder="70" min="20" max="350" step="0.1" />
            </label>
            <label className="pc-field">
              <span className="pc-field-label">Height (cm)</span>
              <input type="number" value={heightCm} onChange={(e) => setHeightCm(e.target.value)} placeholder="170" min="80" max="260" step="0.1" />
            </label>
          </div>
          <label className="pc-field">
            <span className="pc-field-label">Address</span>
            <input type="text" value={address} onChange={(e) => setAddress(e.target.value)} placeholder="Street, city, state" />
          </label>
        </div>

        {error && <p className="pc-error">{error}</p>}

        <div className="pc-actions">
          <button className="pc-btn pc-btn-primary" onClick={save} disabled={saving}>
            {saving ? "Saving..." : "Save changes"}
          </button>
          <button className="pc-btn pc-btn-secondary" onClick={cancel} disabled={saving}>Cancel</button>
        </div>
      </div>
    );
  }

  return (
    <div className="pc">
      <div className="pc-header">
        <div className="pc-avatar">{initial}</div>
        <div>
          <div className="pc-name">{user.name}</div>
          <div className="pc-email">{user.email}</div>
        </div>
      </div>

      <div className="pc-stats">
        {user.age != null && (
          <div className="pc-stat">
            <div className="pc-stat-label">
              <svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="18" rx="2" ry="2"></rect><line x1="16" y1="2" x2="16" y2="6"></line><line x1="8" y1="2" x2="8" y2="6"></line><line x1="3" y1="10" x2="21" y2="10"></line></svg>
              AGE
            </div>
            <div className="pc-stat-value">{user.age}</div>
          </div>
        )}
        {user.weight_kg != null && (
          <div className="pc-stat">
            <div className="pc-stat-label">
              <svg viewBox="0 0 24 24"><path d="M6.5 6.5h11l1 11H5.5z"></path><circle cx="12" cy="4.5" r="2.5"></circle></svg>
              WEIGHT
            </div>
            <div className="pc-stat-value">{user.weight_kg} <span>kg</span></div>
          </div>
        )}
        {user.height_cm != null && (
          <div className="pc-stat">
            <div className="pc-stat-label">
              <svg viewBox="0 0 24 24"><path d="M12 2v20M8 6l4-4 4 4M8 18l4 4 4-4"></path></svg>
              HEIGHT
            </div>
            <div className="pc-stat-value">{user.height_cm} <span>cm</span></div>
          </div>
        )}
      </div>

      {user.address && (
        <div className="pc-section">
          <div className="pc-section-label">
            <svg viewBox="0 0 24 24"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"></path><circle cx="12" cy="10" r="3"></circle></svg>
            ADDRESS
          </div>
          <div className="pc-section-text">{user.address}</div>
        </div>
      )}

      {user.dob && (
        <div className="pc-section">
          <div className="pc-section-label">
            <svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="18" rx="2" ry="2"></rect><line x1="16" y1="2" x2="16" y2="6"></line><line x1="8" y1="2" x2="8" y2="6"></line><line x1="3" y1="10" x2="21" y2="10"></line></svg>
            DATE OF BIRTH
          </div>
          <div className="pc-section-text">{formatDateDisplay(user.dob)}</div>
        </div>
      )}

      <button className="pc-edit-btn" onClick={startEditing}>
        <svg viewBox="0 0 24 24"><path d="M17 3a2.8 2.8 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5z"></path></svg>
        Edit profile
      </button>

      <button className="pc-delete-btn" onClick={handleDelete}>Delete account</button>

      {error && <p className="pc-error">{error}</p>}
    </div>
  );
}
