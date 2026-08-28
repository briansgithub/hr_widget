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
    private var service: HeartRateService? = null
    private var bound = false

    private val uiCallback = object : HeartRateService.UICallback {
        override fun onBpmUpdate(bpm: Int) {}
        override fun onStatusUpdate(status: String) {}
        override fun onDiscordStatusUpdate(status: String) {}
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

        recyclerView = findViewById(R.id.serverRecyclerView)
        recyclerView.layoutManager = LinearLayoutManager(this)

        val intent = Intent(this, HeartRateService::class.java)
        bindService(intent, connection, Context.BIND_AUTO_CREATE)
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
        service?.let {
            recyclerView.adapter = ServerAdapter(it.getGuilds()) { id, enabled ->
                it.toggleGuild(id, enabled)
            }
        }
    }

    class ServerAdapter(
        private val guilds: List<HeartRateService.GuildInfo>,
        private val onToggle: (String, Boolean) -> Unit
    ) : RecyclerView.Adapter<ServerAdapter.ViewHolder>() {

        class ViewHolder(view: View) : RecyclerView.ViewHolder(view) {
            val nameText: TextView = view.findViewById(R.id.guildName)
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
            holder.switch.isChecked = guild.isEnabled
            holder.switch.setOnCheckedChangeListener { _, isChecked ->
                onToggle(guild.id, isChecked)
            }
        }

        override fun getItemCount() = guilds.size
    }
}
