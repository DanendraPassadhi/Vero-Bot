# 📋 Discord To-Do List Bot v2.0

Bot Discord untuk manajemen to-do list dengan reminder otomatis, pagination interaktif, custom reminders, dan fitur kolaborasi tim.

## ✨ Fitur Utama

### 📝 Basic Features

- ✅ **Tambah tugas** dengan `/add` (judul, deadline, deskripsi, tag: individu/kelompok)
- ✅ **Tampilkan tugas** dengan `/list` (pagination otomatis jika >5 tugas, dengan jam spesifik)
- ✅ **Edit tugas** dengan `/edit` (ubah judul, deadline, deskripsi, atau tag)
- ✅ **Tandai selesai** dengan `/done` (ID atau nomor dari `/list`)
- ✅ **Tambah event** dengan `/addevent` (rapat, meeting, dll dengan waktu mulai & selesai)
- ✅ **Tampilkan event** dengan `/listevent` (pemisahan event dan task, dengan jam spesifik)
- ✅ **Tandai event selesai** dengan `/doneevent` (ID atau nomor dari `/listevent`)
- ✅ **Set timezone** per user dengan `/settimezone` (default: Asia/Jakarta)
- ✅ **Set channel** reminder dengan `/setchannel` (Admin only, pisah channel untuk task & event)

### 🚀 Advanced Features (v2.0)

- 🔘 **Interactive Pagination** - Navigasi tugas dengan tombol Previous/Next/Refresh
- ⏰ **Custom Reminders** - Set waktu reminder khusus per tugas (`/setreminder`)
- 👥 **Assign Tasks** - Assign tugas kelompok ke multiple users (`/assign`)
- 📊 **Shared Task List** - Lihat semua tugas kelompok di server (`/listkelompok`)
- 📅 **Weekly Summary** - Rekap tugas mingguan otomatis setiap Minggu 20:00
- 🎨 **Rich Embeds** - UI cantik dengan emoji, color-coded urgency, thumbnails

### 🔔 Reminder System

- **Otomatis:** 72h, 24h, 5h sebelum deadline
- **Custom:** Set waktu reminder sesuka kamu (format: `1d`, `3h`, `30m` atau `YYYY-MM-DD HH:MM`)
- **Deadline:** Notifikasi saat deadline tercapai
- **Auto-delete:** Tugas overdue >24h otomatis dihapus

---

## 🛠️ Teknologi

- **discord.py** - Slash commands & interactive UI (buttons, views)
- **pymongo** - MongoDB Atlas untuk database
- **APScheduler** - Background scheduler untuk reminders
- **zoneinfo** - Timezone support (IANA)
- **Python 3.11+** - Async/await, modern Python features

## 📂 Project Structure (Modular)

```
Vero-Bot/
├── bot.py              # Main bot file (commands, event handlers, reminders)
├── config.py           # Configuration & constants
├── database.py         # MongoDB collection definitions
├── utils.py            # Helper functions (date formatting, timezone handling)
├── views.py            # Discord UI components (pagination views)
├── requirements.txt    # Python dependencies
├── .env.example        # Environment variables template
└── README.md           # This file
```

**Fitur Modular:**

- Separated tasks & events management
- Clean date formatting (Indonesian: "Senin 30 Desember 2025")
- Time display with HH:MM format (e.g., "10:30 - 12:00")

---

## 📦 Instalasi

### 1. Clone Repository

```bash
git clone <repository-url>
cd to-do-list
```

### 2. Setup Environment

```pwsh
# Buat virtual environment
python -m venv .venv

# Activate (Windows)
.\.venv\Scripts\Activate.ps1

# Activate (Linux/Mac)
source .venv/bin/activate
```

### 3. Install Dependencies

```pwsh
pip install -r requirements.txt
```

### 4. Configure Environment Variables

Buat file `.env` berdasarkan `.env.example`:

```env
DISCORD_TOKEN=your_discord_bot_token
MONGO_URI=your_mongodb_atlas_connection_string
MONGO_DB=todo_bot
DEFAULT_TZ=Asia/Jakarta
DEV_GUILDS=your_guild_id  # Optional, untuk fast command sync
```

**MongoDB Atlas Setup:**

- Buat cluster di [MongoDB Atlas](https://www.mongodb.com/cloud/atlas)
- Whitelist IP: `0.0.0.0/0` (untuk Railway.app atau dynamic IPs)
- Copy connection string ke `MONGO_URI`

### 5. Run Bot

```pwsh
python bot.py
```

Bot akan:

- Connect ke MongoDB
- Sync slash commands ke Discord
- Start APScheduler untuk reminders
- Log status di console

---

## 🎮 Command List

| Command         | Description                           | Example                                                                                         |
| --------------- | ------------------------------------- | ----------------------------------------------------------------------------------------------- |
| `/add`          | Tambah tugas baru                     | `/add judul:"Tugas" tanggal_deadline:"2025-12-25 10:00" tag:kelompok`                           |
| `/list`         | Tampilkan tugas (dengan pagination)   | `/list`                                                                                         |
| `/edit`         | Edit tugas existing                   | `/edit identifier:1`                                                                            |
| `/done`         | Tandai tugas selesai                  | `/done identifier:1`                                                                            |
| `/addevent`     | Tambah event baru (acara/meeting)     | `/addevent judul:"Meeting" tanggal_mulai:"2025-12-25 10:00" tanggal_selesai:"2025-12-25 12:00"` |
| `/listevent`    | Tampilkan event (dengan pagination)   | `/listevent`                                                                                    |
| `/doneevent`    | Tandai event selesai                  | `/doneevent identifier:1`                                                                       |
| `/assign`       | Assign tugas ke users (kelompok only) | `/assign identifier:1 users:@User1 @User2`                                                      |
| `/listkelompok` | Lihat semua tugas kelompok di server  | `/listkelompok`                                                                                 |
| `/setreminder`  | Set custom reminder                   | `/setreminder identifier:1 waktu:1d`                                                            |
| `/settimezone`  | Set timezone personal                 | `/settimezone tz:Asia/Jakarta`                                                                  |
| `/setchannel`   | Set channel untuk reminders (Admin)   | `/setchannel channel:#reminders tag:task`                                                       |
| `/ping`         | Check bot status & latency            | `/ping`                                                                                         |
| `/help`         | Tampilkan panduan lengkap             | `/help`                                                                                         |

---

## 📖 Quick Start Guide

### 1. Setup Personal Preferences

```
/settimezone tz:Asia/Jakarta
/setchannel (pilih channel untuk reminders)
```

### 2. Tambah Tugas Individu

```
/add judul:"Belajar Python" tanggal_deadline:"2025-12-20 14:00" deskripsi:"Chapter 5-7" tag:individu
```

### 3. Tambah Tugas Kelompok & Assign

```
/add judul:"Presentasi Final" tanggal_deadline:"2025-12-30 09:00" tag:kelompok
/assign identifier:1 users:@John @Jane @Mike
```

### 4. Set Custom Reminder

```
/setreminder identifier:1 waktu:7d    # 7 hari sebelum deadline
/setreminder identifier:1 waktu:2025-12-23 08:00    # Waktu spesifik
```

### 5. Lihat Tugas

```
/list                 # Tugas personal
/listkelompok        # Semua tugas kelompok di server
```

### 6. Navigate dengan Pagination

Jika tugas >5, gunakan tombol:

- ⬅️ **Previous** - Halaman sebelumnya
- ➡️ **Next** - Halaman berikutnya
- 🔄 **Refresh** - Reload data terbaru

### 7. Selesaikan Tugas

```
/done identifier:1    # Gunakan nomor dari /list atau ID task
```

---

## 🎯 Use Cases

### Personal Task Management

- Track tugas individu dengan deadline
- Get reminders 72h, 24h, 5h sebelum deadline
- Set custom reminders sesuai kebutuhan
- Auto-delete tugas overdue

### Team Collaboration

- Buat tugas kelompok dengan tag `kelompok`
- Assign ke multiple team members
- Lihat progress semua tugas kelompok di `/listkelompok`
- Weekly summary setiap Minggu untuk recap

### Study Groups

- Track assignment deadlines bersama
- Assign tasks ke anggota grup
- Get automated reminders
- Weekly recap untuk review progress

---

## 📊 Database Schema

### Collection: `tasks`

```javascript
{
    "_id": ObjectId,
    "user_id": int,           // Discord user ID (owner)
    "guild_id": int,          // Discord server ID
    "judul": string,
    "deskripsi": string,
    "deadline": datetime,     // Stored in UTC
    "status": boolean,        // false = active, true = completed
    "tag": string,            // "individu" or "kelompok"
    "assigned_users": [       // Array of Discord user IDs
        user_id_1,
        user_id_2
    ],
    "custom_reminders": [     // Array of custom reminder times
        {
            "time": datetime,
            "sent": boolean,
            "created_by": user_id
        }
    ],
    "reminders_sent": [       // Track sent reminders
        "rem_72h",
        "rem_24h",
        "rem_5h",
        "rem_due"
    ]
}
```

### Collection: `user_settings`

```javascript
{
    "user_id": int,
    "timezone": string        // IANA timezone (e.g., "Asia/Jakarta")
}
```

### Collection: `guild_settings`

```javascript
{
    "guild_id": int,
    "channel_id_task": int,   // Channel for task reminders
    "channel_id_event": int   // Channel for event reminders
}
```

---

## 🔧 Advanced Configuration

### Custom Reminder Format

**Duration format:**

- `1m` - 1 menit
- `1h` - 1 jam
- `1d` - 1 hari
- `1w` - 1 minggu

**Absolute format:**

- `YYYY-MM-DD HH:MM` - Waktu spesifik (timezone user)

### Color-Coded Urgency

- 🔴 **< 24 hours** - Urgent (Red)
- 🟡 **24-72 hours** - Warning (Yellow/Orange)
- 🟢 **> 72 hours** - Safe (Green)

### Auto-Delete Logic

- Tugas overdue **>24 hours** otomatis dihapus
- Grace period 24h untuk late submission
- Prevent database bloat

---

## 📚 Documentation

Untuk dokumentasi lengkap, lihat file berikut:

- **`UPDATE_SUMMARY.md`** - Technical summary & implementation details
- **`QUICK_START.md`** - Panduan lengkap untuk end-users
- **`TESTING_CHECKLIST.md`** - Testing checklist untuk developers

---

## 🚀 Deployment

### Railway.app (Recommended)

1. Push code ke GitHub repository
2. Connect repository ke Railway.app
3. Add environment variables di Railway dashboard
4. Deploy otomatis setiap push
5. Bot akan restart dan online 24/7

### Manual Deployment

```bash
# Install dependencies
pip install -r requirements.txt

# Run with nohup (Linux)
nohup python bot.py &

# Run with screen (Linux)
screen -S todobot
python bot.py
# Ctrl+A+D to detach
```

---

## 🐛 Troubleshooting

### Commands Not Showing

- Wait 1 hour for global sync
- Or use `DEV_GUILDS` for instant sync in test server

### Reminders Not Sending

- Check `/setchannel` is set
- Verify bot has Send Messages permission
- Check MongoDB connection in logs

### Pagination Not Working

- Ensure tasks > 5
- Refresh with 🔄 button
- Check for overdue tasks (auto-filtered)

### Weekly Summary Not Sent

- Verify bot is online Sunday 20:00
- Check logs for errors
- Test manually by editing scheduler

---

## 📄 License

This project is licensed under the MIT License.

---

## 🤝 Contributing

Contributions are welcome! Please:

1. Fork repository
2. Create feature branch
3. Commit changes
4. Push to branch
5. Open pull request

---

## 📞 Support

Need help? Check:

- `/help` command in Discord
- `QUICK_START.md` for user guide
- GitHub Issues for bug reports

---

## 🎉 Changelog

### v2.1 (2025-12-31) - Events & Modular Update

- ✅ **Separated Tasks & Events** - Distinct commands for tasks vs events
- ✅ **Event Management** - `/addevent`, `/listevent`, `/doneevent` commands
- ✅ **Time Display** - Show specific HH:MM times in list & listevent
- ✅ **Code Modularization** - Split into config.py, database.py, utils.py, views.py
- ✅ **Separate Reminder Channels** - Different channels for task & event reminders
- ✅ **Backward Compatibility** - Handle old/new event field names gracefully
- ✅ **Bug Fixes** - Fixed /listevent KeyError when accessing old field names

### v2.0 (2025-11-18)

- ✅ Added interactive pagination with buttons
- ✅ Added custom reminder system (`/setreminder`)
- ✅ Added task assignment feature (`/assign`)
- ✅ Added shared task list (`/listkelompok`)
- ✅ Added weekly summary automation
- ✅ Updated `/help` with all new commands
- ✅ Database schema extended (assigned_users, custom_reminders)

### v1.0 (Initial Release)

- ✅ Basic CRUD operations (add, list, edit, done)
- ✅ Automated reminders (72h, 24h, 5h, due)
- ✅ Timezone support
- ✅ Rich embeds UI
- ✅ Tag system (individu/kelompok)

---

**Bot Version:** v2.1  
**Last Updated:** 2025-12-31  
**Status:** ✅ Production Ready
