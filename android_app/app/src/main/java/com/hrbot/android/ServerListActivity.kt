package com.hrbot.android

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.os.Bundle
import android.os.IBinder
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.google.android.material.switchmaterial.SwitchMaterial

class ServerListActivity : AppCompatActivity() {

    private lateinit var recyclerView: RecyclerView
    private lateinit var emptyText: TextView
    private var service: HeartRateService? = null
    private var bound = false

    private val uiCallback = object : HeartRateService.UICallback {
        override fun onBpmUpdate(bpm: Int) {}
        override fun onStatusUpdate(status: String) {}
        override fun onDiscordStatusUpdate(status: String) {
            runOnUiThread { updateList() }
        }
        override fun onLog(message: String) {}
        override fun onGuildsUpdated(guilds: List<HeartRateService.GuildInfo>) {
            runOnUiThread { updateList() }
        }
    }

    private val connection = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName?, binder: IBinder?) {
            val localBinder = binder as HeartRateService.LocalBinder
            service = localBinder.getService()
            bound = true
            service?.addCallback(uiCallback)
            updateList()
        }

        override fun onServiceDisconnected(name: ComponentName?) {
            service?.removeCallback(uiCallback)
            service = null
            bound = false
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_server_list)
        supportActionBar?.setDisplayHomeAsUpEnabled(true)
        title = "Discord servers"

        recyclerView = findViewById(R.id.serverRecyclerView)
        emptyText = findViewById(R.id.serverListEmpty)
        recyclerView.layoutManager = LinearLayoutManager(this)

        val intent = Intent(this, HeartRateService::class.java)
        bindService(intent, connection, Context.BIND_AUTO_CREATE)
    }

    override fun onSupportNavigateUp(): Boolean {
        finish()
        return true
    }

    override fun onDestroy() {
        super.onDestroy()
        if (bound) {
            service?.removeCallback(uiCallback)
            unbindService(connection)
            bound = false
        }
    }

    private fun updateList() {
        val svc = service ?: return
        val guilds = svc.getGuilds().sortedBy { it.name.lowercase() }
        val discordOn = svc.isDiscordEnabled

        if (guilds.isEmpty()) {
            emptyText.visibility = View.VISIBLE
            emptyText.text = if (discordOn) {
                "Connected, but no servers have loaded yet. Wait a moment or check that the bot was invited."
            } else {
                "No servers yet. Turn on Discord Bot on the main screen and wait for servers to load."
            }
            recyclerView.adapter = ServerAdapter(emptyList()) { _, _ -> }
            return
        }

        emptyText.visibility = View.GONE
        recyclerView.adapter = ServerAdapter(guilds) { id, enabled ->
            svc.toggleGuild(id, enabled)
            // Refresh hints for the row that changed without waiting for a service event.
            updateList()
        }
    }

    class ServerAdapter(
        private val guilds: List<HeartRateService.GuildInfo>,
        private val onToggle: (String, Boolean) -> Unit
    ) : RecyclerView.Adapter<ServerAdapter.ViewHolder>() {

        class ViewHolder(view: View) : RecyclerView.ViewHolder(view) {
            val nameText: TextView = view.findViewById(R.id.guildName)
            val hintText: TextView = view.findViewById(R.id.guildHint)
            val switch: SwitchMaterial = view.findViewById(R.id.guildSwitch)
        }

        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): ViewHolder {
            val view = LayoutInflater.from(parent.context)
                .inflate(R.layout.item_server, parent, false)
            return ViewHolder(view)
        }

        override fun onBindViewHolder(holder: ViewHolder, position: Int) {
            val guild = guilds[position]
            holder.nameText.text = guild.name
            holder.hintText.text = if (guild.isEnabled) {
                "Nickname shows live BPM"
            } else {
                "Nickname stays BPMsworth"
            }
            holder.switch.setOnCheckedChangeListener(null)
            holder.switch.isChecked = guild.isEnabled
            holder.switch.setOnCheckedChangeListener { _, isChecked ->
                onToggle(guild.id, isChecked)
            }
        }

        override fun getItemCount() = guilds.size
    }
}
