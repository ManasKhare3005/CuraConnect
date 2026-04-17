import { useState } from "react";
import { updateProfile, deleteAccount } from "./api";

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
    if (!window.confirm("Are you sure you want to delete your account? All your data will be permanently removed.")) {
      return;
    }
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
      const updates = {
        name: name.trim() || null,
        dob: dob || null,
        weight_kg: weightKg !== "" ? parseFloat(weightKg) : null,
        height_cm: heightCm !== "" ? parseFloat(heightCm) : null,
        address: address.trim() || null,
      };
      const updated = await updateProfile(token, updates);
      onUpdate(updated);
      setEditing(false);
    } catch (err) {
      setError(err.message || "Failed to save.");
    } finally {
      setSaving(false);
    }
  }

  if (editing) {
    return (
      <div className="profile-card">
        <h3>Edit Profile</h3>
        <label className="profile-field">
          Name
          <input type="text" value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <label className="profile-field">
          Date of Birth
          <input
            type="date"
            value={dob}
            onChange={(e) => setDob(e.target.value)}
            max={new Date().toISOString().split("T")[0]}
          />
        </label>
        <label className="profile-field">
          Weight (kg)
          <input
            type="number"
            value={weightKg}
            onChange={(e) => setWeightKg(e.target.value)}
            placeholder="e.g. 70"
            min="20"
            max="350"
            step="0.1"
          />
        </label>
        <label className="profile-field">
          Height (cm)
          <input
            type="number"
            value={heightCm}
            onChange={(e) => setHeightCm(e.target.value)}
            placeholder="e.g. 170"
            min="80"
            max="260"
            step="0.1"
          />
        </label>
        <label className="profile-field">
          Address
          <input
            type="text"
            value={address}
            onChange={(e) => setAddress(e.target.value)}
            placeholder="Street, city, state"
          />
        </label>
        {error && <p className="profile-error">{error}</p>}
        <div className="profile-actions">
          <button className="btn btn-primary btn-sm" onClick={save} disabled={saving}>
            {saving ? "Saving..." : "Save"}
          </button>
          <button className="btn btn-secondary btn-sm" onClick={cancel} disabled={saving}>
            Cancel
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="profile-card">
      <h3>Profile</h3>
      <div className="profile-row">
        <span className="profile-label">Name</span>
        <span className="profile-value">{user.name}</span>
      </div>
      <div className="profile-row">
        <span className="profile-label">Email</span>
        <span className="profile-value">{user.email}</span>
      </div>
      {user.age != null && (
        <div className="profile-row">
          <span className="profile-label">Age</span>
          <span className="profile-value">{user.age}</span>
        </div>
      )}
      {user.dob && (
        <div className="profile-row">
          <span className="profile-label">DOB</span>
          <span className="profile-value">{user.dob}</span>
        </div>
      )}
      {user.weight_kg != null && (
        <div className="profile-row">
          <span className="profile-label">Weight</span>
          <span className="profile-value">{user.weight_kg} kg</span>
        </div>
      )}
      {user.height_cm != null && (
        <div className="profile-row">
          <span className="profile-label">Height</span>
          <span className="profile-value">{user.height_cm} cm</span>
        </div>
      )}
      {user.address && (
        <div className="profile-row">
          <span className="profile-label">Address</span>
          <span className="profile-value">{user.address}</span>
        </div>
      )}
      <div className="profile-bottom-actions">
        <button className="btn btn-secondary btn-sm profile-edit-btn" onClick={startEditing}>
          Edit Profile
        </button>
        <button className="btn-delete btn-sm" onClick={handleDelete}>
          Delete Account
        </button>
      </div>
      {error && <p className="profile-error">{error}</p>}
    </div>
  );
}
