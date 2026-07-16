import { useState, useRef, useEffect } from 'react'
import './Sidebar.css'

export default function Sidebar({ conversations, activeId, onSelect, onNew, onDelete, onRename, open }) {
  const [editingId, setEditingId] = useState(null)
  const [editValue, setEditValue] = useState('')
  const editRef = useRef(null)

  useEffect(() => {
    if (editingId) editRef.current?.focus()
  }, [editingId])

  const startRename = (conv, e) => {
    e.stopPropagation()
    setEditingId(conv.id)
    setEditValue(conv.title)
  }

  const commitRename = (id) => {
    const trimmed = editValue.trim()
    if (trimmed) onRename(id, trimmed)
    setEditingId(null)
  }

  const handleRenameKey = (e, id) => {
    if (e.key === 'Enter') commitRename(id)
    if (e.key === 'Escape') setEditingId(null)
  }

  if (!open) return null

  return (
    <aside className="sidebar">
      <div className="sidebar-header">
        <button className="sidebar-new-btn" onClick={onNew}>
          + New Chat
        </button>
      </div>

      <div className="sidebar-list">
        {conversations.length === 0 && (
          <p className="sidebar-empty">No conversations yet.</p>
        )}
        {conversations.map(conv => (
          <div
            key={conv.id}
            className={`sidebar-item ${conv.id === activeId ? 'active' : ''}`}
            onClick={() => onSelect(conv.id)}
            title={conv.title}
          >
            {editingId === conv.id ? (
              <input
                ref={editRef}
                className="sidebar-rename-input"
                value={editValue}
                onChange={e => setEditValue(e.target.value)}
                onBlur={() => commitRename(conv.id)}
                onKeyDown={e => handleRenameKey(e, conv.id)}
                onClick={e => e.stopPropagation()}
              />
            ) : (
              <>
                <span
                  className="sidebar-title"
                  onDoubleClick={e => startRename(conv, e)}
                >
                  {conv.title}
                </span>
                <button
                  className="sidebar-delete-btn"
                  onClick={e => { e.stopPropagation(); onDelete(conv.id) }}
                  title="Delete conversation"
                >
                  ✕
                </button>
              </>
            )}
          </div>
        ))}
      </div>
    </aside>
  )
}
