/*
 * Small synchronous FIFO (default 4 x 8 bits). `dout` shows the oldest entry.
 * SPDX-License-Identifier: Apache-2.0
 */
`default_nettype none

module pw_fifo #(
    parameter W  = 8,
    parameter AW = 2
) (
    input  wire         clk,
    input  wire         rst_n,
    input  wire         clr,
    input  wire         wr,
    input  wire [W-1:0] din,
    input  wire         rd,
    output wire [W-1:0] dout,
    output wire         empty,
    output wire         full
);
  reg [W-1:0] mem[0:(1<<AW)-1];
  reg [AW:0] wp, rp;

  wire [AW:0] count = wp - rp;
  assign empty = (wp == rp);
  assign full  = (count[AW] == 1'b1);
  assign dout  = mem[rp[AW-1:0]];

  always @(posedge clk) begin
    if (!rst_n || clr) begin
      wp <= 0;
      rp <= 0;
    end else begin
      if (wr && !full) begin
        mem[wp[AW-1:0]] <= din;
        wp <= wp + 1'b1;
      end
      if (rd && !empty) rp <= rp + 1'b1;
    end
  end
endmodule
