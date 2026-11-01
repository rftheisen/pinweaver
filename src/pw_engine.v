/*
 * PinWeaver pin engine: a tiny CPU that runs one 16-bit instruction per tick.
 * See docs/isa.md for the instruction set.
 * SPDX-License-Identifier: Apache-2.0
 */
`default_nettype none

module pw_engine (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        enable,
    input  wire        restart,
    // config
    input  wire [15:0] clkdiv,
    input  wire [2:0]  out_base,
    input  wire [3:0]  out_count,
    input  wire [2:0]  set_base,
    input  wire [2:0]  set_count,
    input  wire [2:0]  side_base,
    input  wire [1:0]  side_count,
    input  wire        side_en,
    input  wire        side_pindirs,  // side-set drives output enables instead of values
    input  wire [2:0]  in_base,
    input  wire [2:0]  jmp_pin,
    input  wire        out_shr,
    input  wire        in_shr,
    input  wire        autopull,
    input  wire        autopush,
    input  wire [3:0]  pull_thresh,
    input  wire [3:0]  push_thresh,
    input  wire [6:0]  wrap_bottom,
    input  wire [6:0]  wrap_top,
    input  wire [6:0]  start_pc,
    input  wire [15:0] crc_poly,
    input  wire [15:0] crc_init,
    input  wire        crc_on_out,   // every OUT bit feeds the CRC
    input  wire        crc_on_in,    // every IN bit feeds the CRC
    // line coder (OUT pins / IN pins with 1 bit); see docs/isa.md
    input  wire        lc_nrzi,      // NRZI: a 0 bit toggles the line
    input  wire        lc_diff,      // drive / read a differential pair (pin, pin + 1)
    input  wire        lc_bitclk,    // coded IN / OUT wait for the bit clock
    input  wire        lc_resync,    // edges on the IN pin re-align the bit clock
    input  wire [2:0]  lc_stuff_n,   // bit stuffing after this many bits (0 = off)
    input  wire        lc_stuff_any, // 0: count 1s only (USB), 1: count equal bits (CAN)
    input  wire [7:0]  lc_bit_m1,    // bit clock period - 1, in ticks
    input  wire [7:0]  lc_sample,    // bit clock phase that fires the coded IN / OUT
    // instruction memory
    output wire [6:0]  pc_o,
    input  wire [15:0] instr,
    // pins
    input  wire [7:0]  pins_in,
    output reg  [7:0]  pins_out,
    output reg  [7:0]  pins_oe,
    // TX FIFO (host -> engine)
    input  wire        tx_empty,
    input  wire [7:0]  tx_data,
    output reg         tx_pop,
    // RX FIFO (engine -> host)
    input  wire        rx_full,
    output reg         rx_push,
    output reg  [7:0]  rx_data,
    // shared flags
    input  wire [3:0]  flags_in,
    output reg  [3:0]  flag_set,
    output reg  [3:0]  flag_clr
);

  // ---------------------------------------------------------------- state
  reg [6:0]  pc;         // bit 6 = page; jumps stay inside the page
  reg [7:0]  x, y, isr, osr;
  reg [3:0]  isr_cnt, osr_cnt;  // bits shifted in / out (0..8)
  reg [3:0]  dly;
  reg [15:0] div;
  reg        to_flag;
  reg [7:0]  prev_pins;
  reg [15:0] crc;
  reg        crc_hold;   // stop feeding the CRC (until crc init)
  reg        lvl;        // line coder: last line level (1 = idle / J)
  reg        last;       // line coder: last data bit
  reg [2:0]  run;        // line coder: equal bits in a row
  reg [7:0]  ph;         // bit clock phase

  assign pc_o = pc;

  wire tick = enable && !restart && (div == 16'd0);
  wire exec = tick && (dly == 4'd0);

  // ---------------------------------------------------------------- decode
  wire [2:0] op  = instr[15:13];
  wire [3:0] ds  = instr[12:9];
  wire [8:0] arg = instr[8:0];

  wire [1:0] nside = side_count + {1'b0, side_en};
  reg  [3:0] delay_v;
  reg  [1:0] side_v;
  wire       side_valid = (side_count != 2'd0) && (!side_en || ds[3]);

  always @(*) begin
    case (nside)
      2'd0:    delay_v = ds;
      2'd1:    delay_v = {1'b0, ds[2:0]};
      2'd2:    delay_v = {2'b0, ds[1:0]};
      default: delay_v = {3'b0, ds[0]};
    endcase
    if (side_en) side_v = (side_count == 2'd2) ? ds[2:1] : {1'b0, ds[2]};
    else         side_v = (side_count == 2'd2) ? ds[3:2] : {1'b0, ds[3]};
  end

  // pins seen from in_base (rotate right)
  function [7:0] rotr(input [7:0] v, input [2:0] n);
    rotr = (v >> n) | (v << (4'd8 - {1'b0, n}));
  endfunction
  wire [7:0] pins_rot = rotr(pins_in, in_base);
  wire [7:0] prev_rot = rotr(prev_pins, in_base);

  wire [3:0] nbits  = (arg[3:0] == 4'd0 || arg[3:0] > 4'd8) ? 4'd8 : arg[3:0];
  wire [7:0] nmask  = 8'hFF >> (4'd8 - nbits);
  wire [3:0] pull_t = (pull_thresh == 4'd0 || pull_thresh > 4'd8) ? 4'd8 : pull_thresh;
  wire [3:0] push_t = (push_thresh == 4'd0 || push_thresh > 4'd8) ? 4'd8 : push_thresh;

  wire [7:0] status = {5'b0, to_flag, rx_full, tx_empty};

  // line coder
  wire line_mode = lc_nrzi || lc_diff || lc_bitclk || (lc_stuff_n != 3'd0);
  wire strobe    = (ph == lc_sample);
  wire stuff_hit = (lc_stuff_n != 3'd0) && (run == lc_stuff_n);
  wire stuff_bit = lc_stuff_any ? ~last : 1'b0;
  wire se0       = lc_diff && !pins_rot[0] && !pins_rot[1];
  wire rx_bit    = lc_nrzi ? (pins_rot[0] == lvl) : pins_rot[0];

  function [2:0] next_run(input [2:0] r, input l, input b, input any);
    begin
      if (any ? (b == l) : b) next_run = (r == 3'd7) ? 3'd7 : r + 3'd1;
      else                    next_run = any ? 3'd1 : 3'd0;
    end
  endfunction

  // write `count` bits of `data` into `cur`, starting at pin `base` (wraps)
  function [7:0] map_pins(input [7:0] cur, input [7:0] data, input [2:0] base, input [3:0] count);
    integer i;
    reg [2:0] idx;
    begin
      map_pins = cur;
      for (i = 0; i < 8; i = i + 1) begin
        idx = base + i[2:0];
        if (i < count) map_pins[idx] = data[i];
      end
    end
  endfunction

  // Feed bits[0], bits[1], ... bits[n-1] into the 16-bit bit-serial CRC.
  // Shorter CRCs live in the top bits (poly and init shifted left).
  function [15:0] crc_feed(input [15:0] c_in, input [7:0] bits, input [3:0] n, input [15:0] poly);
    integer i;
    reg [15:0] c;
    begin
      c = c_in;
      for (i = 0; i < 8; i = i + 1)
        if (i < n) c = (c << 1) ^ ((bits[i] ^ c[15]) ? poly : 16'd0);
      crc_feed = c;
    end
  endfunction

  function [7:0] bitrev(input [7:0] v);
    integer i;
    begin
      for (i = 0; i < 8; i = i + 1) bitrev[i] = v[7-i];
    end
  endfunction

  // ---------------------------------------------------------------- execute
  reg [6:0] n_pc;
  reg [7:0] n_x, n_y, n_isr, n_osr, n_out, n_oe;
  reg [3:0] n_isr_cnt, n_osr_cnt, n_dly;
  reg       n_to;
  reg [15:0] n_crc;
  reg        f_en, f_lsb;   // CRC feed this tick
  reg        n_hold, n_lvl, n_last, emit, e_bit, e_lvl, line_io;
  reg [2:0]  n_run;
  reg [7:0]  n_ph;
  reg [7:0]  f_bits;
  reg       done, jump, cond, need;
  reg [6:0] jt;
  reg [7:0] src_v, t8, o_src, o_new;
  reg [4:0] c5;

  always @(*) begin
    n_pc = pc; n_x = x; n_y = y; n_isr = isr; n_osr = osr;
    n_out = pins_out; n_oe = pins_oe;
    n_isr_cnt = isr_cnt; n_osr_cnt = osr_cnt; n_dly = dly; n_to = to_flag; n_crc = crc;
    f_en = 1'b0; f_lsb = 1'b1; f_bits = 8'd0;
    n_hold = crc_hold; n_lvl = lvl; n_last = last; n_run = run; n_ph = ph;
    emit = 1'b0; e_bit = 1'b0; e_lvl = 1'b0; line_io = 1'b0;
    done = 1'b1; jump = 1'b0; cond = 1'b0; need = 1'b0; jt = 7'd0;
    src_v = 8'd0; t8 = 8'd0; o_src = 8'd0; o_new = 8'd0; c5 = 5'd0;
    tx_pop = 1'b0; rx_push = 1'b0; rx_data = isr; flag_set = 4'd0; flag_clr = 4'd0;

    if (tick && dly != 4'd0) n_dly = dly - 4'd1;
    if (tick) begin
      if (lc_resync && (pins_rot[0] ^ prev_rot[0])) n_ph = 8'd1;
      else n_ph = (ph == lc_bit_m1) ? 8'd0 : ph + 8'd1;
    end

    if (exec) begin
      case (op)
        3'd0: begin  // JMP
          case (arg[8:6])
            3'd0: cond = 1'b1;
            3'd1: cond = (x == 8'd0);
            3'd2: begin cond = (x != 8'd0); n_x = x - 8'd1; end
            3'd3: cond = (y == 8'd0);
            3'd4: begin cond = (y != 8'd0); n_y = y - 8'd1; end
            3'd5: cond = (x != y);
            3'd6: cond = pins_in[jmp_pin];
            default: cond = to_flag;
          endcase
          if (cond) begin jump = 1'b1; jt = {pc[6], arg[5:0]}; end
        end

        3'd1: begin  // WAIT
          case (arg[7:6])
            2'd0: cond = (pins_rot[arg[2:0]] == arg[8]);
            2'd1: cond = (pins_in[arg[2:0]] == arg[8]);
            2'd2: cond = (flags_in[arg[1:0]] == arg[8]);
            default: cond = arg[8] ? (pins_rot[arg[2:0]] & ~prev_rot[arg[2:0]])
                                   : (~pins_rot[arg[2:0]] & prev_rot[arg[2:0]]);
          endcase
          if (cond) begin
            n_to = 1'b0;
            if (arg[7:6] == 2'd2 && arg[8]) flag_clr[arg[1:0]] = 1'b1;
          end else if (arg[5]) begin
            if (y == 8'd0) n_to = 1'b1;
            else begin n_y = y - 8'd1; done = 1'b0; end
          end else begin
            done = 1'b0;
          end
        end

        3'd2: begin  // IN
          case (arg[8:6])
            3'd0: src_v = pins_rot;
            3'd1: src_v = x;
            3'd2: src_v = y;
            3'd4: src_v = osr;
            default: src_v = 8'd0;
          endcase
          line_io = line_mode && arg[8:6] == 3'd0 && nbits == 4'd1;
          if (line_io) begin
            n_lvl  = pins_rot[0];
            n_last = rx_bit;
            n_run  = next_run(run, last, rx_bit, lc_stuff_any);
            src_v  = {7'd0, rx_bit};
          end
          if (line_io && lc_bitclk && !strobe) begin
            done = 1'b0;                      // wait for the bit clock
            n_lvl = lvl; n_last = last; n_run = run;
          end else if (line_io && se0) begin
            n_to = 1'b1;                      // end of packet: no bit
            n_lvl = lvl; n_last = last; n_run = run;
          end else if (line_io && stuff_hit) begin
            jump = 1'b1; jt = pc;             // a stuff bit: drop it, read again
          end else begin
          if (line_io) n_to = 1'b0;
          if (in_shr) t8 = (isr >> nbits) | (src_v << (4'd8 - nbits));
          else        t8 = (isr << nbits) | (src_v & nmask);
          c5 = {1'b0, isr_cnt} + {1'b0, nbits};
          if (c5 > 5'd8) c5 = 5'd8;
          if (autopush && c5[3:0] >= push_t) begin
            if (rx_full) done = 1'b0;
            else begin rx_push = 1'b1; rx_data = t8; n_isr = 8'd0; n_isr_cnt = 4'd0; end
          end else begin
            n_isr = t8; n_isr_cnt = c5[3:0];
          end
          if (done && crc_on_in && !crc_hold) begin f_en = 1'b1; f_bits = src_v; f_lsb = in_shr; end
          if (!done) begin n_lvl = lvl; n_last = last; n_run = run; n_to = to_flag; end  // RX FIFO full: retry
          end
        end

        3'd3: begin  // OUT
          need = autopull && (osr_cnt >= pull_t);
          line_io = line_mode && arg[8:6] == 3'd0;
          if (line_io && lc_bitclk && !strobe) begin
            done = 1'b0;                      // wait for the bit clock
          end else if (line_io && stuff_hit) begin
            emit = 1'b1; e_bit = stuff_bit;   // send a stuff bit, then this OUT again
            jump = 1'b1; jt = pc;
          end else if (need && tx_empty) begin
            done = 1'b0;
          end else begin
            if (need) tx_pop = 1'b1;
            o_src = need ? tx_data : osr;
            c5 = (need ? 5'd0 : {1'b0, osr_cnt}) + {1'b0, nbits};
            if (c5 > 5'd8) c5 = 5'd8;
            if (out_shr) begin t8 = o_src & nmask;           o_new = o_src >> nbits; end
            else         begin t8 = o_src >> (4'd8 - nbits); o_new = o_src << nbits; end
            n_osr = o_new; n_osr_cnt = c5[3:0];
            if (crc_on_out && !crc_hold) begin f_en = 1'b1; f_bits = t8; f_lsb = out_shr; end
            case (arg[8:6])
              3'd0: if (line_io && nbits == 4'd1) begin emit = 1'b1; e_bit = t8[0]; end
                    else n_out = map_pins(pins_out, t8, out_base, out_count);
              3'd1: n_x = t8;
              3'd2: n_y = t8;
              3'd4: n_oe = map_pins(pins_oe, t8, out_base, out_count);
              3'd5: begin jump = 1'b1; jt = {pc[6], t8[5:0]}; end
              3'd6: begin n_isr = t8; n_isr_cnt = nbits; end
              default: ;
            endcase
          end
        end

        3'd4: begin  // PUSH / PULL
          if (!arg[8]) begin
            if (rx_full) begin
              if (arg[7]) done = 1'b0;
            end else begin
              rx_push = 1'b1; rx_data = isr; n_isr = 8'd0; n_isr_cnt = 4'd0;
            end
          end else begin
            if (tx_empty) begin
              if (arg[7]) done = 1'b0;
              else begin n_osr = x; n_osr_cnt = 4'd0; end
            end else begin
              tx_pop = 1'b1; n_osr = tx_data; n_osr_cnt = 4'd0;
            end
          end
        end

        3'd5: begin  // MOV
          case (arg[2:0])
            3'd0: src_v = pins_rot;
            3'd1: src_v = x;
            3'd2: src_v = y;
            3'd3: src_v = isr;
            3'd4: src_v = osr;
            3'd5: src_v = status;
            3'd6: src_v = {4'd0, flags_in};
            default: src_v = 8'd0;
          endcase
          case (arg[5:4])
            2'd0: t8 = src_v;
            2'd1: t8 = ~src_v;
            2'd2: t8 = bitrev(src_v);
            default: t8 = src_v + 8'd1;
          endcase
          case (arg[8:6])
            3'd0: n_out = map_pins(pins_out, t8, out_base, out_count);
            3'd1: n_x = t8;
            3'd2: n_y = t8;
            3'd3: begin n_isr = t8; n_isr_cnt = 4'd0; end
            3'd4: begin n_osr = t8; n_osr_cnt = 4'd0; end
            3'd5: begin jump = 1'b1; jt = {pc[6], t8[5:0]}; end
            3'd6: n_oe = map_pins(pins_oe, t8, out_base, out_count);
            default: ;
          endcase
        end

        3'd6: begin  // SET
          case (arg[8:6])
            3'd0: n_out = map_pins(pins_out, {3'd0, arg[4:0]}, set_base, {1'b0, set_count});
            3'd1: n_x = {3'd0, arg[4:0]};
            3'd2: n_y = {3'd0, arg[4:0]};
            3'd3: n_oe = map_pins(pins_oe, {3'd0, arg[4:0]}, set_base, {1'b0, set_count});
            3'd4: if (arg[4]) flag_set[arg[1:0]] = 1'b1; else flag_clr[arg[1:0]] = 1'b1;
            default: ;
          endcase
        end

        default: begin  // CRC
          case (arg[8:6])
            3'd0: begin n_crc = crc_init; n_hold = 1'b0; end
            3'd1: n_x = crc[7:0];
            3'd2: n_y = crc[15:8];
            3'd3: begin n_osr = crc[7:0];  n_osr_cnt = 4'd0; end
            3'd4: begin n_osr = crc[15:8]; n_osr_cnt = 4'd0; end
            3'd5: begin f_en = 1'b1; f_bits = x; end
            3'd6: begin n_lvl = 1'b1; n_last = 1'b0; n_run = 3'd0; n_ph = 8'd0; end  // line reset
            default: n_hold = 1'b1;                                                  // crc hold
          endcase
        end
      endcase

      // line coder output (a coded OUT bit or a stuff bit)
      if (emit) begin
        e_lvl  = lc_nrzi ? (e_bit ? lvl : ~lvl) : e_bit;
        n_lvl  = e_lvl;
        n_last = e_bit;
        n_run  = next_run(run, last, e_bit, lc_stuff_any);
        if (lc_diff) n_out = map_pins(pins_out, {6'd0, ~e_lvl, e_lvl}, out_base, 4'd2);
        else         n_out = map_pins(pins_out, {7'd0, e_lvl}, out_base, 4'd1);
      end

      // one shared CRC unit
      if (f_en) n_crc = crc_feed(crc, f_lsb ? f_bits : bitrev(f_bits << (4'd8 - nbits)), nbits, crc_poly);

      if (side_valid) begin
        if (side_pindirs) n_oe  = map_pins(n_oe,  {6'd0, side_v}, side_base, {2'd0, side_count});
        else              n_out = map_pins(n_out, {6'd0, side_v}, side_base, {2'd0, side_count});
      end

      if (done) begin
        n_dly = delay_v;
        if (jump)                  n_pc = jt;
        else if (pc == wrap_top)   n_pc = wrap_bottom;
        else                       n_pc = {pc[6], pc[5:0] + 6'd1};  // wraps inside the page
      end
    end
  end

  // ---------------------------------------------------------------- registers
  always @(posedge clk) begin
    if (!rst_n) begin
      pc <= 7'd0; x <= 8'd0; y <= 8'd0; isr <= 8'd0; osr <= 8'd0;
      isr_cnt <= 4'd0; osr_cnt <= 4'd8; dly <= 4'd0; div <= 16'd0;
      to_flag <= 1'b0; prev_pins <= 8'd0; pins_out <= 8'd0; pins_oe <= 8'd0;
      crc <= 16'd0; crc_hold <= 1'b0; lvl <= 1'b1; last <= 1'b0; run <= 3'd0; ph <= 8'd0;
    end else if (restart) begin
      pc <= start_pc; x <= 8'd0; y <= 8'd0; isr <= 8'd0; osr <= 8'd0;
      isr_cnt <= 4'd0; osr_cnt <= 4'd8; dly <= 4'd0; div <= 16'd0;
      to_flag <= 1'b0; prev_pins <= pins_in;
      crc <= crc_init; crc_hold <= 1'b0; lvl <= 1'b1; last <= 1'b0; run <= 3'd0; ph <= 8'd0;
    end else begin
      if (enable) div <= (div == 16'd0) ? clkdiv : div - 16'd1;
      if (tick) begin
        prev_pins <= pins_in;
        pc <= n_pc; x <= n_x; y <= n_y; isr <= n_isr; osr <= n_osr;
        isr_cnt <= n_isr_cnt; osr_cnt <= n_osr_cnt; dly <= n_dly; to_flag <= n_to;
        pins_out <= n_out; pins_oe <= n_oe; crc <= n_crc;
        crc_hold <= n_hold; lvl <= n_lvl; last <= n_last; run <= n_run; ph <= n_ph;
      end
    end
  end

endmodule
