/*
 * PinWeaver: a programmable protocol emulator.
 * Two pin engines share a 64-word instruction memory; a host loads programs
 * and moves data over SPI. See docs/isa.md.
 * SPDX-License-Identifier: Apache-2.0
 */
`default_nettype none

module tt_um_pinweaver (
    input  wire [7:0] ui_in,    // [0] SCK, [1] MOSI, [2] CS_n
    output wire [7:0] uo_out,   // [0] MISO, [1] e0 RX ready, [2] e1 RX ready, [6:3] flags
    input  wire [7:0] uio_in,   // GPIO 0-7 (input path)
    output wire [7:0] uio_out,  // GPIO 0-7 (output path)
    output wire [7:0] uio_oe,   // GPIO 0-7 (1 = output)
    input  wire       ena,
    input  wire       clk,
    input  wire       rst_n
);

  // ------------------------------------------------------------ host SPI
  wire       miso, active, byte_start, byte_valid, first_byte;
  wire [7:0] byte_data, resp, status;

  wire        imem_we, cfg_we;
  wire [5:0]  imem_addr;
  wire [15:0] imem_data;
  wire [7:0]  cfg_addr, cfg_data, tx_data;
  wire [1:0]  en, restart, tx_push, rx_pop;
  wire [3:0]  host_fset, host_fclr;

  wire [1:0]  tx_empty, tx_full, rx_empty, rx_full;
  wire [7:0]  rx_head0, rx_head1;
  wire [5:0]  pc0, pc1;
  reg  [3:0]  flags;

  assign status = {~rx_empty[1], tx_full[1], ~rx_empty[0], tx_full[0], flags};

  pw_spi_slave u_spi (
      .clk(clk), .rst_n(rst_n),
      .sck_i(ui_in[0]), .mosi_i(ui_in[1]), .csn_i(ui_in[2]), .miso(miso),
      .active(active), .byte_start(byte_start), .byte_valid(byte_valid),
      .byte_data(byte_data), .first_byte(first_byte), .resp(resp), .status(status)
  );

  pw_host u_host (
      .clk(clk), .rst_n(rst_n),
      .active(active), .byte_start(byte_start), .byte_valid(byte_valid),
      .byte_data(byte_data), .first_byte(first_byte), .resp(resp),
      .imem_we(imem_we), .imem_addr(imem_addr), .imem_data(imem_data),
      .cfg_we(cfg_we), .cfg_addr(cfg_addr), .cfg_data(cfg_data),
      .en(en), .restart(restart),
      .tx_push(tx_push), .tx_data(tx_data), .rx_pop(rx_pop),
      .rx_head({rx_head1, rx_head0}), .rx_empty(rx_empty),
      .dbg({status, 2'b00, pc1, 2'b00, pc0}),
      .flag_set(host_fset), .flag_clr(host_fclr)
  );

  // ------------------------------------------------------------ instruction memory
  reg [15:0] imem[0:63];
  always @(posedge clk) if (imem_we) imem[imem_addr] <= imem_data;
  wire [15:0] instr0 = imem[pc0];
  wire [15:0] instr1 = imem[pc1];

  // ------------------------------------------------------------ config registers
  reg [7:0] cfg[0:31];
  integer k;
  always @(posedge clk) begin
    if (!rst_n) begin
      for (k = 0; k < 32; k = k + 1) cfg[k] <= ((k % 16) == 9) ? 8'd63 : 8'd0;
    end else if (cfg_we && cfg_addr[7:5] == 3'd0) begin
      cfg[cfg_addr[4:0]] <= cfg_data;
    end
  end

  // ------------------------------------------------------------ GPIO input sync
  reg [7:0] pin_s1, pin_s2;
  always @(posedge clk) begin
    if (!rst_n) begin pin_s1 <= 8'd0; pin_s2 <= 8'd0; end
    else begin pin_s1 <= uio_in; pin_s2 <= pin_s1; end
  end

  // ------------------------------------------------------------ engines
  wire [7:0] out0, oe0, out1, oe1, rxd0, rxd1, txd0, txd1;
  wire       pop0, pop1, push0, push1;
  wire [3:0] fs0, fc0, fs1, fc1;

  pw_fifo u_tx0 (.clk(clk), .rst_n(rst_n), .clr(restart[0]), .wr(tx_push[0]), .din(tx_data),
                 .rd(pop0), .dout(txd0), .empty(tx_empty[0]), .full(tx_full[0]));
  pw_fifo u_rx0 (.clk(clk), .rst_n(rst_n), .clr(restart[0]), .wr(push0), .din(rxd0),
                 .rd(rx_pop[0]), .dout(rx_head0), .empty(rx_empty[0]), .full(rx_full[0]));
  pw_fifo u_tx1 (.clk(clk), .rst_n(rst_n), .clr(restart[1]), .wr(tx_push[1]), .din(tx_data),
                 .rd(pop1), .dout(txd1), .empty(tx_empty[1]), .full(tx_full[1]));
  pw_fifo u_rx1 (.clk(clk), .rst_n(rst_n), .clr(restart[1]), .wr(push1), .din(rxd1),
                 .rd(rx_pop[1]), .dout(rx_head1), .empty(rx_empty[1]), .full(rx_full[1]));

  pw_engine eng0 (
      .clk(clk), .rst_n(rst_n), .enable(en[0]), .restart(restart[0]),
      .clkdiv({cfg[1], cfg[0]}),
      .out_base(cfg[2][2:0]), .out_count(cfg[2][6:3]),
      .set_base(cfg[3][2:0]), .set_count(cfg[3][5:3]),
      .side_base(cfg[4][2:0]), .side_count(cfg[4][4:3]), .side_en(cfg[4][5]), .side_pindirs(cfg[4][6]),
      .in_base(cfg[5][2:0]), .jmp_pin(cfg[5][5:3]),
      .out_shr(cfg[6][0]), .in_shr(cfg[6][1]), .autopull(cfg[6][2]), .autopush(cfg[6][3]),
      .pull_thresh(cfg[7][3:0]), .push_thresh(cfg[7][7:4]),
      .wrap_bottom(cfg[8][5:0]), .wrap_top(cfg[9][5:0]), .start_pc(cfg[10][5:0]),
      .pc_o(pc0), .instr(instr0),
      .pins_in(pin_s2), .pins_out(out0), .pins_oe(oe0),
      .tx_empty(tx_empty[0]), .tx_data(txd0), .tx_pop(pop0),
      .rx_full(rx_full[0]), .rx_push(push0), .rx_data(rxd0),
      .flags_in(flags), .flag_set(fs0), .flag_clr(fc0)
  );

  pw_engine eng1 (
      .clk(clk), .rst_n(rst_n), .enable(en[1]), .restart(restart[1]),
      .clkdiv({cfg[17], cfg[16]}),
      .out_base(cfg[18][2:0]), .out_count(cfg[18][6:3]),
      .set_base(cfg[19][2:0]), .set_count(cfg[19][5:3]),
      .side_base(cfg[20][2:0]), .side_count(cfg[20][4:3]), .side_en(cfg[20][5]), .side_pindirs(cfg[20][6]),
      .in_base(cfg[21][2:0]), .jmp_pin(cfg[21][5:3]),
      .out_shr(cfg[22][0]), .in_shr(cfg[22][1]), .autopull(cfg[22][2]), .autopush(cfg[22][3]),
      .pull_thresh(cfg[23][3:0]), .push_thresh(cfg[23][7:4]),
      .wrap_bottom(cfg[24][5:0]), .wrap_top(cfg[25][5:0]), .start_pc(cfg[26][5:0]),
      .pc_o(pc1), .instr(instr1),
      .pins_in(pin_s2), .pins_out(out1), .pins_oe(oe1),
      .tx_empty(tx_empty[1]), .tx_data(txd1), .tx_pop(pop1),
      .rx_full(rx_full[1]), .rx_push(push1), .rx_data(rxd1),
      .flags_in(flags), .flag_set(fs1), .flag_clr(fc1)
  );

  // ------------------------------------------------------------ shared flags
  always @(posedge clk) begin
    if (!rst_n) flags <= 4'd0;
    else flags <= (flags & ~(fc0 | fc1 | host_fclr)) | fs0 | fs1 | host_fset;
  end

  // ------------------------------------------------------------ outputs
  assign uio_out = (out0 & oe0) | (out1 & oe1);
  assign uio_oe  = oe0 | oe1;
  assign uo_out  = {1'b0, flags, ~rx_empty[1], ~rx_empty[0], miso};

  wire _unused = &{ena, ui_in[7:3], 1'b0};

endmodule
